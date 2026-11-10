"""Visão dos nodes (workers): capacidade alocável, requests e limits somados por node.

Fonte: métricas do kube-state-metrics, consultadas como valores instantâneos (agora).
- request é o que o scheduler reserva. Ele não agenda pod se a soma dos requests passar do alocável,
  então requests acima de 100% indicam um node fora do normal (cordon, capacidade reduzida, pod sem agendamento).
- limit é o teto de uso. Soma de limits acima de 100% do alocável é overcommit: se todos os pods
  usarem até o limit ao mesmo tempo, o node não comporta. É o overcommit normal de um cluster, não um erro.
- Requests ≥ NODE_HIGH_PCT do alocável deixam pouca folga para agendar novos pods.
- Desbalanceamento: diferença entre o maior e o menor percentual de requests entre os workers > NODE_SPREAD_PP.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from .analysis import MIB, iso

CONTROL_ROLES = {"control-plane", "master"}
NODE_HIGH_PCT = 85.0    # requests a partir deste percentual do alocável: "requests altos"
NODE_SPREAD_PP = 20.0   # diferença (pontos percentuais) entre o node mais e o menos carregado

_RUNNING = 'and on(namespace,pod) (kube_pod_status_phase{phase="Running"} == 1)'
QUERIES = {
    "info": "kube_node_info",
    "roles": "kube_node_role",
    "alloc": 'kube_node_status_allocatable{resource=~"cpu|memory"}',
    "ready": 'kube_node_status_condition{condition="Ready",status="true"} == 1',
    "unsched": "kube_node_spec_unschedulable == 1",
    "pods": 'kube_pod_info{node!=""}',
    "requests": ('max by (node,namespace,pod,container,resource)'
                 f'(kube_pod_container_resource_requests{{resource=~"cpu|memory",node!=""}} {_RUNNING})'),
    "limits": ('max by (node,namespace,pod,container,resource)'
               f'(kube_pod_container_resource_limits{{resource=~"cpu|memory",node!=""}} {_RUNNING})'),
}


def collect_nodes(client, now):
    """Consultas instantâneas em paralelo. Devolve os resultados brutos por nome de consulta."""
    with ThreadPoolExecutor(max_workers=client.max_concurrency) as pool:
        futures = {k: pool.submit(client.query, q, now) for k, q in QUERIES.items()}
        return {k: f.result() for k, f in futures.items()}


def _num(item):
    try:
        return float(item["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _scale(res):
    """CPU em cores → millicores; memória em bytes → Mi."""
    return 1000.0 if res == "cpu" else 1 / MIB


def _r(value, digits=1):
    return None if value is None else round(value, digits)


def _pct(value, total):
    if value is None or not total:
        return None
    return round(value / total * 100, 1)


def _new_node(name):
    return {
        "name": name, "roles": set(), "alloc": {}, "ready": None, "unschedulable": False, "pods": set(),
        "req": defaultdict(float), "lim": defaultdict(float), "containers": set(), "limited": set(),
    }


def _index(raw, excluded):
    """Agrupa as séries brutas por node (ignorando namespaces excluídos)."""
    nodes = {}

    def node(name):
        return nodes.setdefault(name, _new_node(name))

    for item in raw["info"]:
        node(item["metric"]["node"])
    for item in raw["roles"]:
        node(item["metric"]["node"])["roles"].add(item["metric"].get("role", ""))
    for item in raw["alloc"]:
        m, value = item["metric"], _num(item)
        if value is not None:
            node(m["node"])["alloc"][m["resource"]] = value
    for item in raw["unsched"]:
        node(item["metric"]["node"])["unschedulable"] = True
    for item in raw["pods"]:
        m = item["metric"]
        if m.get("namespace") not in excluded:
            node(m["node"])["pods"].add((m["namespace"], m["pod"]))
    for kind in ("requests", "limits"):
        for item in raw[kind]:
            m, value = item["metric"], _num(item)
            if value is None or m.get("namespace") in excluded:
                continue
            n = node(m["node"])
            container = (m["namespace"], m["pod"], m["container"])
            n["containers"].add(container)
            if kind == "requests":
                n["req"][m["resource"]] += value
            else:
                n["lim"][m["resource"]] += value
                n["limited"].add((container, m["resource"]))
    # ready: se a métrica de condição não existe, o estado é desconhecido (None), não "não pronto"
    if raw["ready"]:
        ready = {item["metric"]["node"] for item in raw["ready"]}
        for name, n in nodes.items():
            n["ready"] = name in ready
    return nodes


def _resource(n, res):
    scale = _scale(res)
    alloc = n["alloc"].get(res)
    alloc = None if alloc is None else alloc * scale
    req, lim = n["req"][res] * scale, n["lim"][res] * scale
    return {
        "allocatable": _r(alloc),
        "requests": _r(req),
        "limits": _r(lim),
        "requestsPct": _pct(req, alloc),
        "limitsPct": _pct(lim, alloc),
        "free": _r(alloc - req) if alloc is not None else None,
    }


def _is_worker(n):
    return not (n["roles"] & CONTROL_ROLES)


def _flags(n, cpu, mem, containers_without_limit):
    flags = []
    if n["ready"] is False:
        flags.append("node não está Ready")
    if n["unschedulable"]:
        flags.append("cordon: não recebe pods novos")
    for label, r in (("CPU", cpu), ("memória", mem)):
        if r["requestsPct"] is not None and r["requestsPct"] > 100:
            flags.append(f"requests de {label} acima do alocável ({r['requestsPct']:g}%)")
        elif r["requestsPct"] is not None and r["requestsPct"] >= NODE_HIGH_PCT:
            flags.append(f"requests de {label} altos ({r['requestsPct']:g}%)")
        if r["limitsPct"] is not None and r["limitsPct"] > 100:
            flags.append(f"overcommit de limits de {label} ({r['limitsPct']:g}%)")
    if containers_without_limit:
        flags.append(f"{containers_without_limit} container(s) sem limit de CPU ou memória")
    return flags


def _status(n, cpu, mem, flags):
    over_requests = any(r["requestsPct"] is not None and r["requestsPct"] > 100 for r in (cpu, mem))
    if over_requests or n["ready"] is False:
        return "sobrecarregado"
    return "atencao" if flags else "ok"


def _node_view(n):
    cpu, mem = _resource(n, "cpu"), _resource(n, "memory")
    # containers sem limit de algum recurso: presente nos requests mas sem limit correspondente
    missing = {c for c in n["containers"] for res in ("cpu", "memory") if (c, res) not in n["limited"]}
    without_limit = len(missing)
    flags = _flags(n, cpu, mem, without_limit)
    return {
        "name": n["name"],
        "role": "control-plane" if not _is_worker(n) else "worker",
        "ready": n["ready"],
        "unschedulable": n["unschedulable"],
        "pods": len(n["pods"]),
        "cpu": cpu,
        "memory": mem,
        "containersWithoutLimit": without_limit,
        "flags": flags,
        "status": _status(n, cpu, mem, flags),
    }


def _totals(nodes, res):
    alloc = sum(n[res]["allocatable"] or 0 for n in nodes)
    req = sum(n[res]["requests"] for n in nodes)
    lim = sum(n[res]["limits"] for n in nodes)
    return {
        "allocatable": _r(alloc), "requests": _r(req), "limits": _r(lim), "free": _r(alloc - req),
        "requestsPct": _pct(req, alloc), "limitsPct": _pct(lim, alloc),
    }


def build_nodes(raw, excluded, now):
    """Visão geral: resumo do cluster e um item por node, ordenados pelo nome."""
    indexed = _index(raw, excluded)
    views = [_node_view(indexed[name]) for name in sorted(indexed)]
    workers = [v for v in views if v["role"] == "worker"]
    spread = None
    if len(workers) >= 2 and all(w["cpu"]["requestsPct"] is not None for w in workers):
        pcts = [w["cpu"]["requestsPct"] for w in workers]
        spread = round(max(pcts) - min(pcts), 1)
    return {
        "generatedAt": iso(now),
        "summary": {
            "workers": len(workers),
            "ready": sum(1 for w in workers if w["ready"]),
            "controlPlane": sum(1 for v in views if v["role"] == "control-plane"),
            "cpu": _totals(workers, "cpu"),
            "memory": _totals(workers, "memory"),
            "spreadPct": spread,
            "balanced": None if spread is None else spread <= NODE_SPREAD_PP,
            "thresholds": {"highPct": NODE_HIGH_PCT, "spreadPp": NODE_SPREAD_PP},
        },
        "nodes": views,
    }


def build_node_detail(raw, excluded, name):
    """Containers agendados no node, com request e limit de CPU e memória. None se o node não existe."""
    indexed = _index(raw, excluded)
    if name not in indexed:
        return None
    n = indexed[name]
    values = defaultdict(dict)
    for kind in ("requests", "limits"):
        for item in raw[kind]:
            m, value = item["metric"], _num(item)
            if m.get("node") != name or value is None or m.get("namespace") in excluded:
                continue
            key = (m["namespace"], m["pod"], m["container"])
            values[key][f"{kind}.{m['resource']}"] = round(value * _scale(m["resource"]), 1)
    containers = []
    for key, v in values.items():
        containers.append({
            "namespace": key[0], "pod": key[1], "container": key[2],
            "cpuRequest": v.get("requests.cpu"), "cpuLimit": v.get("limits.cpu"),
            "memoryRequest": v.get("requests.memory"), "memoryLimit": v.get("limits.memory"),
        })
    containers.sort(key=lambda c: -(c["cpuRequest"] or 0))
    cpu, mem = _resource(n, "cpu"), _resource(n, "memory")
    return {
        "node": _node_view(n),
        "pods": len(n["pods"]),
        "cpu": cpu,
        "memory": mem,
        "containers": containers,
    }
