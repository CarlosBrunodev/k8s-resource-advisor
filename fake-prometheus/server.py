"""Prometheus FALSO para validação local. Não usar em clusters reais.

Responde às consultas feitas pelo back-end (query, query_range, runtimeinfo, buildinfo)
com séries sintéticas e determinísticas para cenários conhecidos:

  shop/api         3 réplicas, request CPU 100m com consumo ~200m  -> subdimensionado
  shop/web         2 réplicas, request CPU 1000m com consumo ~150m -> superdimensionado
  shop/worker      StatefulSet sem request/limit                   -> sem configuração
  shop/cache       request/limit próximos do ideal                 -> adequado (ou ±1 faixa)
  shop/oom-app     limit de memória 256Mi e OOMKilled              -> subdimensionado (OOM)
  shop/recent-app  pod criado há 6 h                               -> dados insuficientes
  payments/ledger  2 réplicas + pod antigo recriado; request de CPU mudou 2 dias atrás
  payments/reports lacuna de coleta de 2,5 dias                    -> dados insuficientes
  payments/gateway request de memória muito acima do consumo       -> superdimensionado
"""
import json
import math
import os
import random
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MIB = 1024 * 1024
DAY = 86400
RETENTION = os.environ.get("FAKE_RETENTION", "15d")
PORT = int(os.environ.get("PORT", "9090"))

# ages em segundos atrás; first_age None = existia antes da janela; last_age 0 = ainda existe
WORKLOADS = [
    dict(ns="shop", kind="Deployment", name="api", replicas=3, cpu=0.2, mem=300 * MIB, throttle=0.02,
         req_cpu=0.1, req_mem=256 * MIB, lim_cpu=0.5, lim_mem=512 * MIB, restarts=3),
    dict(ns="shop", kind="Deployment", name="web", replicas=2, cpu=0.15, mem=200 * MIB,
         req_cpu=1.0, req_mem=1024 * MIB, lim_cpu=2.0, lim_mem=2048 * MIB),
    dict(ns="shop", kind="StatefulSet", name="worker", replicas=2, cpu=0.3, mem=400 * MIB),
    dict(ns="shop", kind="Deployment", name="cache", replicas=1, cpu=0.2, mem=300 * MIB,
         req_cpu=0.27, req_mem=360 * MIB, lim_cpu=0.3, lim_mem=420 * MIB),
    dict(ns="shop", kind="Deployment", name="oom-app", replicas=1, cpu=0.1, mem=240 * MIB, oom=True,
         req_cpu=0.12, req_mem=240 * MIB, lim_cpu=0.2, lim_mem=256 * MIB),
    dict(ns="shop", kind="Deployment", name="recent-app", replicas=1, cpu=0.1, mem=100 * MIB,
         req_cpu=0.1, req_mem=128 * MIB, lim_cpu=0.2, lim_mem=256 * MIB, first_age=6 * 3600),
    dict(ns="payments", kind="Deployment", name="ledger", replicas=2, cpu=0.25, mem=500 * MIB,
         req_cpu_history=[(10 * DAY, 0.5), (2 * DAY, 0.3)], req_mem=512 * MIB, lim_cpu=1.0,
         lim_mem=1024 * MIB,
         # pod antigo, já encerrado, de um ReplicaSet anterior do mesmo Deployment
         recreated=[dict(gen=1, idx=9, first_age=4 * DAY, last_age=3 * DAY)]),
    dict(ns="payments", kind="Deployment", name="reports", replicas=1, cpu=0.1, mem=200 * MIB,
         req_cpu=0.2, req_mem=256 * MIB, lim_cpu=0.5, lim_mem=512 * MIB,
         gap=(7 * DAY, 2 * DAY)),
    dict(ns="payments", kind="Deployment", name="gateway", replicas=1, cpu=0.4, mem=300 * MIB,
         req_cpu=0.1, req_mem=512 * MIB, lim_cpu=0.8, lim_mem=1024 * MIB),
]


def _suffix(seed, n):
    return f"{zlib.crc32(seed.encode()) % 10 ** n:0{n}d}"


def _build_pods():
    pods = []
    for w in WORKLOADS:
        def add(pod, rs, idx, first_age, last_age):
            pods.append(dict(
                ns=w["ns"], pod=pod, rs=rs, workload=w, idx=idx,
                owner=("ReplicaSet", rs) if rs else (w["kind"], w["name"]),
                first_age=first_age, last_age=last_age,
            ))

        for i in range(w["replicas"]):
            if w["kind"] == "Deployment":
                rs = f"{w['name']}-{_suffix(w['name'] + '0', 9)}"
                add(f"{rs}-{_suffix(rs + str(i), 5)}", rs, i, w.get("first_age"), 0)
            else:
                add(f"{w['name']}-{i}", None, i, w.get("first_age"), 0)
        for old in w.get("recreated", []):
            rs = f"{w['name']}-{_suffix(w['name'] + str(old['gen']), 9)}"
            add(f"{rs}-{_suffix(rs + str(old['idx']), 5)}", rs, old["idx"], old["first_age"], old["last_age"])
    return pods


PODS = _build_pods()
STEP_DEFAULT = 900

# nodes do cluster simulado: worker-3 é menor, então concentra a pressão de requests
NODES = [
    dict(name="cp-1", role="control-plane", cpu=2.0, mem=4096 * MIB),
    dict(name="worker-1", role="worker", cpu=4.0, mem=8192 * MIB),
    dict(name="worker-2", role="worker", cpu=4.0, mem=8192 * MIB),
    dict(name="worker-3", role="worker", cpu=2.0, mem=4096 * MIB),
]


def _grid(start, end, step):
    t = math.floor(start / step) * step
    out = []
    while t <= end:
        if t >= start:
            out.append(float(t))
        t += step
    return out


def _alive(p, t, now):
    lo = -math.inf if p["first_age"] is None else now - p["first_age"]
    hi = math.inf if p["last_age"] == 0 else now - p["last_age"]
    w = p["workload"]
    if "gap" in w:
        gap_from, gap_to = now - w["gap"][0], now - w["gap"][1]
        if gap_from <= t <= gap_to:
            return False
    return lo <= t <= hi


def _noisy(base, amp_sin, rel, t, key):
    r = random.Random(f"{key}:{int(t)}")
    value = base * (1 + amp_sin * math.sin(2 * math.pi * t / DAY)) * (1 + r.gauss(0, rel))
    return max(value, 0.0), r


def _cpu(p, t):
    w = p["workload"]
    value, r = _noisy(w["cpu"], 0.1, 0.05, t, p["pod"] + "cpu")
    if r.random() < 0.003:  # picos raros, fora do P99
        value *= 1.5
    return value


def _mem(p, t):
    return _noisy(p["workload"]["mem"], 0.05, 0.03, t, p["pod"] + "mem")[0]


def _config(const, history, t, now):
    if history:
        age = now - t
        candidates = [(since, val) for since, val in history if since >= age]
        if candidates:
            return min(candidates)[1]
        return max(history)[1]
    return const


def _config_series(p, metric_res, kind, t, now):
    w = p["workload"]
    if kind == "request":
        if metric_res == "cpu":
            return _config(w.get("req_cpu"), w.get("req_cpu_history"), t, now) if (
                w.get("req_cpu") is not None or w.get("req_cpu_history")) else None
        return w.get("req_mem")
    return w.get("lim_cpu") if metric_res == "cpu" else w.get("lim_mem")


def _range(query, start, end, step, now):
    result = []
    if "kube_replicaset_owner" in query:
        seen = set()
        for p in PODS:
            if p["rs"] and p["rs"] not in seen:
                seen.add(p["rs"])
                result.append({"metric": {"namespace": p["ns"], "replicaset": p["rs"],
                                          "owner_kind": "Deployment", "owner_name": p["workload"]["name"]},
                               "values": []})
        return result
    if "kube_pod_owner" in query:
        for p in PODS:
            kind, name = p["owner"]
            result.append({"metric": {"namespace": p["ns"], "pod": p["pod"],
                                      "owner_kind": kind, "owner_name": name}, "values": []})
        return result

    grid = _grid(start, end, step)
    for p in PODS:
        labels = {"namespace": p["ns"], "pod": p["pod"], "container": "app"}
        pts = [t for t in grid if _alive(p, t, now)]

        def emit(extra, fn):
            values = [[t, repr(float(fn(t)))] for t in pts]
            if values:
                result.append({"metric": {**labels, **extra}, "values": values})

        w = p["workload"]
        if "cfs_throttled_periods_total" in query:
            emit({}, lambda t: max(0.0, w.get("throttle", 0.0) + random.Random(f"{p['pod']}{int(t)}").gauss(0, 0.002)))
        elif "container_cpu_usage_seconds_total" in query:
            emit({}, lambda t: _cpu(p, t))
        elif "container_memory_working_set_bytes" in query:
            emit({}, lambda t: _mem(p, t))
        elif "kube_pod_container_resource_requests" in query:
            for res, field in (("cpu", "req_cpu"), ("memory", "req_mem")):
                if res == "cpu" and not (w.get("req_cpu") is not None or w.get("req_cpu_history")):
                    continue
                if res == "memory" and w.get("req_mem") is None:
                    continue
                emit({"resource": res}, lambda t, res=res: _config_series(p, res, "request", t, now))
        elif "kube_pod_container_resource_limits" in query:
            for res, field in (("cpu", "lim_cpu"), ("memory", "lim_mem")):
                if w.get(field) is not None:
                    emit({"resource": res}, lambda t, res=res: _config_series(p, res, "limit", t, now))
        elif "kube_pod_container_status_last_terminated_reason" in query and w.get("oom"):
            emit({"reason": "OOMKilled"}, lambda t: 1)
    return result


def _node_of(pod):
    """Distribui os pods entre os workers de forma determinística (worker-3 tem menos capacidade)."""
    workers = [n["name"] for n in NODES if n["role"] == "worker"]
    return workers[zlib.crc32(pod.encode()) % len(workers)]


def _node_instant(query, at):
    def vec(metric, value):
        return {"metric": metric, "value": [at, repr(float(value))]}

    if "kube_node_info" in query:
        return [vec({"node": n["name"]}, 1) for n in NODES]
    if "kube_node_role" in query:
        return [vec({"node": n["name"], "role": n["role"]}, 1) for n in NODES]
    if "kube_node_status_allocatable" in query:
        return [vec({"node": n["name"], "resource": res}, n[key])
                for n in NODES for res, key in (("cpu", "cpu"), ("memory", "mem"))]
    if "kube_node_status_condition" in query:
        return [vec({"node": n["name"], "status": "true"}, 1) for n in NODES]
    live = [(p, _node_of(p["pod"])) for p in PODS if _alive(p, at, at)]
    if "kube_pod_info" in query:
        return [vec({"namespace": p["ns"], "pod": p["pod"], "node": node}, 1) for p, node in live]
    out = []
    for p, node in live:
        base = {"node": node, "namespace": p["ns"], "pod": p["pod"], "container": "app"}
        for kind, metric in (("requests", "kube_pod_container_resource_requests"),
                             ("limits", "kube_pod_container_resource_limits")):
            if metric not in query:
                continue
            for res in ("cpu", "memory"):
                value = _config_series(p, res, "request" if kind == "requests" else "limit", at, at)
                if value is not None:
                    out.append(vec({**base, "resource": res}, value))
    return out


def _instant(query, at):
    if "kube_pod_container_status_restarts_total" in query:
        return [{"metric": {"namespace": p["ns"], "pod": p["pod"], "container": "app"},
                 "value": [at, str(p["workload"]["restarts"])]}
                for p in PODS if p["workload"].get("restarts")]
    return _node_instant(query, at)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        now = time.time()
        if url.path == "/api/v1/query_range":
            step = float(q.get("step", STEP_DEFAULT))
            payload = {"status": "success", "data": {"resultType": "matrix", "result": _range(
                q["query"], float(q["start"]), float(q["end"]), step, now)}}
        elif url.path == "/api/v1/query":
            payload = {"status": "success", "data": {"resultType": "vector",
                                                     "result": _instant(q["query"], float(q.get("time", now)))}}
        elif url.path == "/api/v1/status/runtimeinfo":
            payload = {"status": "success", "data": {"storageRetention": RETENTION}}
        elif url.path == "/api/v1/status/buildinfo":
            payload = {"status": "success", "data": {"version": "fake-0.1"}}
        elif url.path in ("/-/ready", "/-/healthy"):
            return self._raw(200, b"Prometheus is Ready.\n", "text/plain")
        else:
            payload = {"status": "error", "errorType": "not_found", "error": "rota inexistente no Prometheus falso"}
            return self._raw(404, json.dumps(payload).encode(), "application/json")
        return self._raw(200, json.dumps(payload).encode(), "application/json")

    def _raw(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"prometheus falso em :{PORT} ({len(PODS)} pods sintéticos, retenção {RETENTION})", flush=True)
    server.serve_forever()
