"""Recomendações, classificação e relatórios a partir das séries coletadas.

Regras (padrão, todas configuráveis por variável de ambiente):
- Consumo de referência: percentil REFERENCE_PERCENTILE (P95) do consumo de cada réplica.
  Para o workload usa-se o MAIOR valor entre as réplicas, para que a média não esconda
  um container com consumo mais alto.
- request sugerido = referência × (1 + REQUEST_MARGIN_PERCENT), com piso mínimo.
- limit sugerido = pico × (1 + LIMIT_MARGIN_PERCENT); CPU usa P99 e memória usa o máximo.
  Nunca fica abaixo do request sugerido.
- Status: compara o valor atual com o sugerido usando TOLERANCE_PERCENT.
  Throttling de CPU acima do limiar e OOMKilled no período classificam o limit como subdimensionado.
"""
import csv
import io
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

MIB = 1024 * 1024
UNITS = {"cpu": "m", "memory": "Mi"}
ITEM_SPECS = (
    ("cpu", "request", "CPU · request"),
    ("cpu", "limit", "CPU · limit"),
    ("memory", "request", "Memória · request"),
    ("memory", "limit", "Memória · limit"),
)
STATUS_LABELS = {
    "adequado": "Adequado",
    "subdimensionado": "Subdimensionado",
    "superdimensionado": "Superdimensionado",
    "sem_configuracao": "Sem configuração",
    "dados_insuficientes": "Dados insuficientes",
}
DEVIATIONS = {"subdimensionado", "superdimensionado"}
WORKLOAD_STATUS_LABELS = {"adequado": "Adequado", "desvio": "Com desvio", "pendente": "Pendente"}
WORKLOAD_STATUSES = set(WORKLOAD_STATUS_LABELS)
ALL_STATUSES = WORKLOAD_STATUSES | set(STATUS_LABELS)
SORT_KEYS = ("ajuste", "pressao", "nome")
CSV_COLUMNS = [
    "Workload", "Tipo de workload", "Container", "Recurso", "Unidade", "Atual", "Consumo de referência",
    "Sugerido", "Ajuste", "Status", "Método", "Janela (dias)", "Namespace", "Filtros",
    "Início do período", "Fim do período", "Atualizado em", "Justificativa",
]
DEFAULT_SORT = "ajuste"


class NotFound(Exception):
    """Recurso inexistente no snapshot atual."""


# --------------------------------------------------------------------------- utilidades

def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1 or pct >= 100:
        return ordered[-1]
    rank = (len(ordered) - 1) * max(0.0, pct) / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _ceil(value):
    # round antes do ceil evita 240.00000000000003 virar 241
    return math.ceil(round(value, 6))


def _round(value, digits=1):
    return None if value is None else round(value, digits)


def _max(stats, key):
    values = [p[key] for p in stats if p[key] is not None]
    return max(values) if values else None


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _pct_label(pct):
    return "máximo" if pct >= 100 else f"P{pct:g}"


def _fmt(value, unit):
    if value is None:
        return "—"
    if unit == "Mi" and value >= 1024:
        return f"{value / 1024:.2f} Gi".replace(".", ",")
    return f"{value:.0f}{unit}"


def resolve_workload(col, namespace, pod):
    """Pod → controlador. ReplicaSets sobem ao Deployment, mesmo após a recriação do pod."""
    kind, name = col.pod_owner.get((namespace, pod), ("Pod", pod))
    if kind == "ReplicaSet":
        if (namespace, name) in col.rs_owner:
            kind, name = col.rs_owner[(namespace, name)]
        else:
            stripped = re.sub(r"-[a-z0-9]{5,10}$", "", name)
            kind, name = ("Deployment", stripped) if stripped != name else ("ReplicaSet", name)
    return kind, name


# --------------------------------------------------------------------------- por pod

def _pod_stats(pod, ser, col, s):
    cpu = [v * 1000 for _, v in ser.cpu]          # m
    mem = [v / MIB for _, v in ser.memory]        # Mi
    thr = [v * 100 for _, v in ser.throttle]      # %
    stamps = [t for t, _ in ser.cpu] + [t for t, _ in ser.memory]
    first, last = min(stamps), max(stamps)
    expected = (min(last, col.window_end) - max(first, col.window_start)) / col.step + 1
    observed = max(len(cpu), len(mem))
    return {
        "pod": pod,
        "first": first,
        "last": last,
        "coverage": min(1.0, observed / max(expected, 1)),
        "cpuPoints": len(cpu),
        "memPoints": len(mem),
        "cpuRef": percentile(cpu, s.reference_percentile),
        "cpuPeak": percentile(cpu, s.cpu_limit_percentile),
        "memRef": percentile(mem, s.reference_percentile),
        "memPeak": percentile(mem, s.memory_limit_percentile),
        "throttlePct": percentile(thr, s.reference_percentile) or 0.0,
        "oomEvents": ser.oom_buckets,
        "restarts": ser.restarts,
    }


def _history(points, scale):
    """Mudanças de valor de request/limit ao longo da janela (configuração vigente em cada momento)."""
    changes, last = [], None
    for ts, value in sorted(points):
        shown = round(value * scale, 1)
        if shown != last:
            changes.append({"at": iso(ts), "value": shown})
            last = shown
    return changes


def _config(pods):
    current, history = {}, {}
    for res, scale in (("cpu", 1000.0), ("memory", 1 / MIB)):
        for kind in ("request", "limit"):
            points, latest = [], None
            for ser in pods.values():
                pts = (ser.requests if kind == "request" else ser.limits)[res]
                points.extend(pts)
                if pts:
                    ts, value = max(pts)
                    if latest is None or ts > latest[0]:
                        latest = (ts, value)
            current[(res, kind)] = round(latest[1] * scale, 1) if latest else None
            history[f"{res}_{kind}"] = _history(points, scale)
    return current, history


def _status(current, suggested, tolerance):
    if current < suggested * (1 - tolerance):
        return "subdimensionado"
    if current > suggested * (1 + tolerance):
        return "superdimensionado"
    return "adequado"


def method_text(res, kind, s, days):
    if kind == "request":
        return f"{days} d · {_pct_label(s.reference_percentile)} · margem {s.request_margin * 100:g}%"
    pct = s.cpu_limit_percentile if res == "cpu" else s.memory_limit_percentile
    return f"{days} d · {_pct_label(pct)} · margem {s.limit_margin * 100:g}%"


def _justification(res, kind, current, ref, sugg, ctx):
    s = ctx["settings"]
    unit = UNITS[res]
    noun = "CPU" if res == "cpu" else "memória"
    if ctx["override"]:
        return ctx["override"]
    if ctx["status"] == "dados_insuficientes":
        return (f"Cobertura de {ctx['coverage'] * 100:.0f}% (mínimo {s.min_coverage * 100:.0f}%) e histórico "
                f"de {ctx['history_hours']:.0f} h (mínimo {s.min_history_hours:g} h). "
                "Dados insuficientes para recomendar um valor.")
    if ctx["status"] == "sem_configuracao" and sugg is None:
        return f"Sem {kind} de {noun} configurado e dados insuficientes para sugerir valor."
    if current is None:
        return f"Sem {kind} de {noun} configurado. Sugestão a partir do consumo observado: {_fmt(sugg, unit)}."
    tol = f"{s.tolerance * 100:g}%"
    if kind == "request":
        return (f"Referência {_fmt(ref, unit)} ({_pct_label(s.reference_percentile)}) × "
                f"(1 + {s.request_margin * 100:g}%) = {_fmt(sugg, unit)}. "
                f"Atual {_fmt(current, unit)}; tolerância ±{tol}.")
    text = (f"Pico {_fmt(ref, unit)} ({_pct_label(s.cpu_limit_percentile if res == 'cpu' else s.memory_limit_percentile)})"
            f" × (1 + {s.limit_margin * 100:g}%) = {_fmt(sugg, unit)}. Atual {_fmt(current, unit)}; tolerância ±{tol}.")
    if ctx["raised_to_request"]:
        text += " Limite elevado para não ficar abaixo do request sugerido."
    return text


# --------------------------------------------------------------------------- por container e workload

def _container(name, pods, col, s, header):
    stats = [_pod_stats(pod, ser, col, s) for pod, ser in pods.items() if ser.cpu or ser.memory]
    if not stats:
        return None
    first = min(p["first"] for p in stats)
    last = max(p["last"] for p in stats)
    history_hours = (last - first) / 3600
    # cobertura = instantes da janela com amostra de ao menos um pod do container (recriações contam, lacunas não)
    window_steps = (col.window_end - col.window_start) / col.step + 1
    observed = {t for ser in pods.values() for t, _ in ser.cpu + ser.memory if t >= col.window_start}
    coverage = min(1.0, len(observed) / window_steps)
    enough = coverage >= s.min_coverage and history_hours >= s.min_history_hours
    cpu_ok = enough and any(p["cpuRef"] is not None for p in stats)
    mem_ok = enough and any(p["memRef"] is not None for p in stats)
    thr = _max(stats, "throttlePct") or 0.0
    oom = sum(p["oomEvents"] for p in stats)

    refs = {
        ("cpu", "request"): _max(stats, "cpuRef"),
        ("cpu", "limit"): _max(stats, "cpuPeak"),
        ("memory", "request"): _max(stats, "memRef"),
        ("memory", "limit"): _max(stats, "memPeak"),
    }
    config, history = _config(pods)

    m, lm = s.request_margin, s.limit_margin
    suggested = {}
    if cpu_ok:
        suggested[("cpu", "request")] = max(s.cpu_floor_m, _ceil(refs[("cpu", "request")] * (1 + m)))
        suggested[("cpu", "limit")] = max(suggested[("cpu", "request")],
                                          _ceil(refs[("cpu", "limit")] * (1 + lm)))
    if mem_ok:
        suggested[("memory", "request")] = max(s.memory_floor_mi, _ceil(refs[("memory", "request")] * (1 + m)))
        suggested[("memory", "limit")] = max(suggested[("memory", "request")],
                                             _ceil(refs[("memory", "limit")] * (1 + lm)))

    items = []
    for res, kind, label in ITEM_SPECS:
        key = (res, kind)
        current = config.get(key)
        data_ok = cpu_ok if res == "cpu" else mem_ok
        sugg = suggested.get(key) if data_ok else None
        ref = refs[key] if data_ok else None
        ctx = {"settings": s, "coverage": coverage, "history_hours": history_hours,
               "override": None, "status": None, "raised_to_request": False}

        if not data_ok:
            status = "sem_configuracao" if current is None else "dados_insuficientes"
        else:
            status = "sem_configuracao" if current is None else _status(current, sugg, s.tolerance)
            if kind == "limit" and current is not None and res == "cpu" and thr >= s.throttling_threshold:
                status = "subdimensionado"
                ctx["override"] = (f"Throttling de CPU em {thr:.1f}% dos períodos (alerta a partir de "
                                   f"{s.throttling_threshold:g}%). Avalie o limit antes de reduzi-lo; "
                                   f"sugestão com pico × (1 + {lm * 100:g}%).")
            if kind == "limit" and current is not None and res == "memory" and oom > 0:
                status = "subdimensionado"
                ctx["override"] = (f"OOMKilled observado em {oom} intervalo(s) da janela. Investigar antes "
                                   f"de reduzir; sugestão com pico × (1 + {lm * 100:g}%).")
            if kind == "limit" and sugg is not None:
                ctx["raised_to_request"] = refs[key] * (1 + lm) < sugg
        ctx["status"] = status

        adjustment = _round(sugg - current) if sugg is not None and current is not None else None
        items.append({
            "container": name,
            "resource": res,
            "kind": kind,
            "label": label,
            "unit": UNITS[res],
            "current": _round(current),
            "reference": _round(ref),
            "suggested": sugg,
            "adjustment": adjustment,
            "status": status,
            "statusLabel": STATUS_LABELS[status],
            "method": method_text(res, kind, s, s.window_days),
            "justification": _justification(res, kind, current, ref, sugg, ctx),
        })

    pods_out = []
    for p in stats:
        pods_out.append({
            "pod": p["pod"],
            "active": p["last"] >= col.window_end - 2 * col.step,
            "firstSeen": iso(p["first"]),
            "lastSeen": iso(p["last"]),
            "coverage": round(p["coverage"], 3),
            "cpuRef": _round(p["cpuRef"]),
            "cpuPeak": _round(p["cpuPeak"]),
            "cpuSuggested": max(s.cpu_floor_m, _ceil(p["cpuRef"] * (1 + m))) if p["cpuRef"] is not None else None,
            "memRef": _round(p["memRef"]),
            "memPeak": _round(p["memPeak"]),
            "memSuggested": (max(s.memory_floor_mi, _ceil(p["memRef"] * (1 + m)))
                             if p["memRef"] is not None else None),
            "throttlingPct": round(p["throttlePct"], 2),
            "oomEvents": p["oomEvents"],
            "restarts": p["restarts"],
        })

    return {
        "name": name,
        "historyHours": round(history_hours, 1),
        "coverage": round(coverage, 3),
        "restarts": sum(p["restarts"] for p in stats),
        "oomEvents": oom,
        "throttlingPct": round(thr, 2),
        "activePods": sum(1 for p in pods_out if p["active"]),
        "config": history,
        "items": items,
        "pods": pods_out,
        "yaml": _yaml(f"{header} · container {name}", items),
    }


def _yaml(header, items):
    lines = [f"# {header} — sugestão para revisão, não aplicada automaticamente", "resources:"]
    for kind in ("request", "limit"):
        chosen = [i for i in items if i["kind"] == kind and i["suggested"] is not None]
        if not chosen:
            continue
        lines.append(f"  {kind}s:")
        for i in chosen:
            key = "cpu" if i["resource"] == "cpu" else "memory"
            lines.append(f"    {key}: {i['suggested']}{i['unit']}")
    return "\n".join(lines)


def _workload_status(items):
    statuses = {i["status"] for i in items}
    if statuses & DEVIATIONS:
        return "desvio"
    if items and statuses == {"adequado"}:
        return "adequado"
    return "pendente"


def _capacity(items, replicas):
    cap = {"cpu": {"reclaim": 0.0, "add": 0.0}, "memory": {"reclaim": 0.0, "add": 0.0}}
    for i in items:
        if i["kind"] != "request" or i["adjustment"] is None:
            continue
        bucket = cap[i["resource"]]
        if i["adjustment"] < 0:
            bucket["reclaim"] += -i["adjustment"] * replicas
        else:
            bucket["add"] += i["adjustment"] * replicas
    return {res: {k: round(v, 1) for k, v in vals.items()} for res, vals in cap.items()}


def _workload(ns, kind, name, containers, col, s):
    summaries = []
    for ctr in sorted(containers):
        summary = _container(ctr, containers[ctr], col, s, f"{kind}/{name}")
        if summary:
            summaries.append(summary)
    if not summaries:
        return None
    items = [i for c in summaries for i in c["items"]]
    replicas = len({p["pod"] for c in summaries for p in c["pods"] if p["active"]})
    history = min(c["historyHours"] for c in summaries)
    return {
        "id": f"{ns}/{kind}/{name}",
        "namespace": ns,
        "kind": kind,
        "name": name,
        "status": _workload_status(items),
        "replicas": replicas,
        "historyHours": history,
        "coverage": min(c["coverage"] for c in summaries),
        "recent": history < s.window_days * 24,
        "capacity": _capacity(items, replicas),
        "containers": summaries,
        "items": items,
    }


def _quality(workloads, col, s):
    items = [i for w in workloads for i in w["items"]]
    containers = [c for w in workloads for c in w["containers"]]
    retention = col.retention_seconds
    return {
        "retentionSeconds": _round(retention, 0),
        "retentionSufficient": None if retention is None else retention >= s.window_days * 86400,
        "workloads": len(workloads),
        "recentWorkloads": sum(1 for w in workloads if w["recent"]),
        "gapContainers": sum(1 for c in containers if c["coverage"] < s.min_coverage),
        "withoutConfig": sum(1 for i in items if i["status"] == "sem_configuracao"),
        "insufficientData": sum(1 for i in items if i["status"] == "dados_insuficientes"),
        "throttledContainers": sum(1 for c in containers if c["throttlingPct"] >= s.throttling_threshold),
        "oomContainers": sum(1 for c in containers if c["oomEvents"] > 0),
    }


def analyze(col, s, now):
    """Gera o snapshot completo (JSON-serializável) a partir das séries coletadas."""
    groups = defaultdict(lambda: defaultdict(dict))
    for (ns, pod, ctr), ser in col.containers.items():
        if ns in s.excluded_namespaces or not (ser.cpu or ser.memory):
            continue
        kind, name = resolve_workload(col, ns, pod)
        groups[(ns, kind, name)][ctr][pod] = ser

    workloads = []
    for (ns, kind, name), containers in sorted(groups.items()):
        wl = _workload(ns, kind, name, containers, col, s)
        if wl:
            workloads.append(wl)

    return {
        "generatedAt": iso(now),
        "window": {"days": s.window_days, "start": iso(col.window_start),
                   "end": iso(col.window_end), "stepSeconds": col.step},
        "quality": _quality(workloads, col, s),
        "workloads": workloads,
    }


# --------------------------------------------------------------------------- visões

def _empty_totals():
    return {"counts": {"adequado": 0, "desvio": 0, "pendente": 0},
            "capacity": {"cpu": {"reclaim": 0.0, "add": 0.0}, "memory": {"reclaim": 0.0, "add": 0.0}}}


def _add_capacity(target, source):
    for res in ("cpu", "memory"):
        for k in ("reclaim", "add"):
            target[res][k] = round(target[res][k] + source[res][k], 1)


def namespace_summaries(snapshot):
    result = {}
    for w in snapshot["workloads"]:
        entry = result.setdefault(w["namespace"], {"name": w["namespace"], "workloads": 0, **_empty_totals()})
        entry["workloads"] += 1
        entry["counts"][w["status"]] += 1
        _add_capacity(entry["capacity"], w["capacity"])
    return [result[k] for k in sorted(result)]


def _item_pressure(i):
    if i["reference"] is not None and i["current"]:
        return i["reference"] / i["current"]
    return 0.0


def _scoped(w, items):
    """Cópia do workload restrita aos itens filtrados, com métricas de ordenação recalculadas."""
    scoped = {k: v for k, v in w.items() if k != "containers"}
    scoped["items"] = items
    scoped["maxAdjustment"] = max((abs(i["adjustment"]) for i in items if i["adjustment"] is not None), default=0.0)
    scoped["pressure"] = round(max((_item_pressure(i) for i in items), default=0.0), 2)
    scoped["capacity"] = _capacity(items, w["replicas"])
    return scoped


_SORTERS = {
    "ajuste": lambda w: (-w["maxAdjustment"], w["name"]),
    "pressao": lambda w: (-w["pressure"], w["name"]),
    "nome": lambda w: w["name"],
}


def filter_workloads(snapshot, namespace, q="", status="", resource="", sort="ajuste"):
    needle = q.strip().lower()
    selected = []
    for w in snapshot["workloads"]:
        if w["namespace"] != namespace:
            continue
        if needle and needle not in f"{w['kind']} {w['name']}".lower():
            continue
        items = [i for i in w["items"] if not resource or i["resource"] == resource]
        if resource and not items:
            continue
        if status:
            matched = w["status"] == status if status in WORKLOAD_STATUSES else any(
                i["status"] == status for i in items)
            if not matched:
                continue
        selected.append(_scoped(w, items))
    selected.sort(key=_SORTERS[sort])
    return selected


def namespace_totals(workloads):
    totals = _empty_totals()
    for w in workloads:
        totals["counts"][w["status"]] += 1
        _add_capacity(totals["capacity"], w["capacity"])
    return totals


def build_charts(col, ns, kind, name):
    """Séries de consumo por container: valor da réplica mais carregada em cada instante."""
    cpu, mem = defaultdict(dict), defaultdict(dict)
    for (n, pod, ctr), ser in col.containers.items():
        if n != ns or resolve_workload(col, n, pod) != (kind, name):
            continue
        for ts, v in ser.cpu:
            cpu[ctr][ts] = max(cpu[ctr].get(ts, 0.0), v * 1000)
        for ts, v in ser.memory:
            mem[ctr][ts] = max(mem[ctr].get(ts, 0.0), v / MIB)

    def series(points):
        return [[int(ts), round(v, 1)] for ts, v in sorted(points.items())]

    return [
        {"container": c, "cpu": {"unit": "m", "consumption": series(cpu.get(c, {}))},
         "memory": {"unit": "Mi", "consumption": series(mem.get(c, {}))}}
        for c in sorted(set(cpu) | set(mem))
    ]


def _num(value):
    return "" if value is None else f"{value:.1f}"


def _signed(value):
    return "" if value is None else f"{value:+.1f}"


def _date(iso_text):
    return datetime.fromisoformat(iso_text).strftime("%d-%m-%Y %H:%M")


def _filter_text(filters):
    # o padrão de ordenação não é um filtro ativo
    active = [f"{k}={v}" for k, v in filters.items() if v and not (k == "sort" and v == DEFAULT_SORT)]
    return "; ".join(active) or "sem filtros"


def export_csv(workloads, snapshot, namespace, filters):
    buf = io.StringIO()
    writer = csv.writer(buf)  # separador vírgula, ponto decimal, sem separador de milhar
    writer.writerow(CSV_COLUMNS)
    win = snapshot["window"]
    filter_text = _filter_text(filters)
    for w in workloads:
        for i in w["items"]:
            writer.writerow([
                w["name"], w["kind"], i["container"], i["label"], i["unit"],
                _num(i["current"]), _num(i["reference"]), _num(i["suggested"]), _signed(i["adjustment"]),
                i["statusLabel"], i["method"], win["days"], namespace, filter_text,
                _date(win["start"]), _date(win["end"]), _date(snapshot["generatedAt"]),
                i["justification"],
            ])
    return buf.getvalue()
