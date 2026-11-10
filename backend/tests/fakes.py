"""Prometheus simulado, API do Kubernetes simulada e configuração de teste."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import Settings
from app.prometheus import PrometheusError

MIB = 1024 * 1024


def make_settings(**overrides):
    base = dict(
        prometheus_url="http://fake", prometheus_bearer_token=None, prometheus_username=None,
        prometheus_password=None, prometheus_timeout=5.0, prometheus_max_concurrency=2,
        window_days=7, step_seconds=900, reference_percentile=95.0, cpu_limit_percentile=99.0,
        memory_limit_percentile=100.0, request_margin=0.2, limit_margin=0.2, tolerance=0.1,
        throttling_threshold=5.0, min_coverage=0.8, min_history_hours=24.0, cache_ttl=300.0,
        excluded_namespaces=("kube-system",), cpu_floor_m=100, memory_floor_mi=32, cpu_limit_floor_m=250,
    )
    base.update(overrides)
    return Settings(**base)


class FakeProm:
    max_concurrency = 2

    def __init__(self, retention="15d"):
        self.pods = []
        self.rs_owners = []
        self.nodes = []
        self.retention = retention
        self.fail = False

    def add_node(self, name, cpu, mem, role="worker", ready=True, unschedulable=False):
        """cpu em cores e mem em bytes (alocável)."""
        self.nodes.append(dict(name=name, cpu=cpu, mem=mem, role=role, ready=ready, unschedulable=unschedulable))

    def add_pod(self, ns, pod, container="app", *, deployment="api", rs=None, cpu=0.0, mem=0.0,
                req_cpu=None, req_mem=None, lim_cpu=None, lim_mem=None,
                first=None, last=None, oom=0, throttle=0.0, restarts=0, node=None):
        """cpu em cores, mem/req_mem/lim_mem em bytes; first/last delimitam a vida do pod (None = sem limite)."""
        self.pods.append(dict(ns=ns, pod=pod, container=container, deployment=deployment, rs=rs,
                              cpu=cpu, mem=mem, req_cpu=req_cpu, req_mem=req_mem, lim_cpu=lim_cpu,
                              lim_mem=lim_mem, first=first, last=last, oom=oom, throttle=throttle,
                              restarts=restarts, node=node))

    def add_rs(self, ns, rs, deployment):
        self.rs_owners.append((ns, rs, deployment))

    @staticmethod
    def _grid(start, end, step, first, last):
        lo = start if first is None else max(start, first)
        hi = end if last is None else min(end, last)
        points, t = [], start
        while t <= end + 1e-6:
            if lo - 1e-6 <= t <= hi + 1e-6:
                points.append(t)
            t += step
        return points

    def query_range(self, promql, start, end, step):
        if self.fail:
            raise PrometheusError("Prometheus indisponível (simulado)")
        if "kube_replicaset_owner" in promql:
            return [{"metric": {"namespace": ns, "replicaset": rs, "owner_kind": "Deployment",
                                "owner_name": dep}, "values": []} for ns, rs, dep in self.rs_owners]
        if "kube_pod_owner" in promql:
            out = []
            for p in self.pods:
                kind, name = ("ReplicaSet", p["rs"]) if p["rs"] else ("StatefulSet", p["deployment"])
                out.append({"metric": {"namespace": p["ns"], "pod": p["pod"],
                                       "owner_kind": kind, "owner_name": name}, "values": []})
            return out

        out = []
        for p in self.pods:
            points = self._grid(start, end, step, p["first"], p["last"])
            labels = {"namespace": p["ns"], "pod": p["pod"], "container": p["container"]}

            def emit(extra, value):
                out.append({"metric": {**labels, **extra},
                            "values": [[t, repr(float(value))] for t in points]})

            if "cfs_throttled_periods_total" in promql:
                emit({}, p["throttle"])
            elif "container_cpu_usage_seconds_total" in promql:
                emit({}, p["cpu"])
            elif "container_memory_working_set_bytes" in promql:
                emit({}, p["mem"])
            elif "kube_pod_container_resource_requests" in promql:
                if p["req_cpu"] is not None:
                    emit({"resource": "cpu"}, p["req_cpu"])
                if p["req_mem"] is not None:
                    emit({"resource": "memory"}, p["req_mem"])
            elif "kube_pod_container_resource_limits" in promql:
                if p["lim_cpu"] is not None:
                    emit({"resource": "cpu"}, p["lim_cpu"])
                if p["lim_mem"] is not None:
                    emit({"resource": "memory"}, p["lim_mem"])
            elif "kube_pod_container_status_last_terminated_reason" in promql and p["oom"]:
                emit({"reason": "OOMKilled"}, 1)
        return out

    def query(self, promql, at):
        if self.fail:
            raise PrometheusError("Prometheus indisponível (simulado)")
        if "kube_pod_container_status_restarts_total" in promql:
            return [{"metric": {"namespace": p["ns"], "pod": p["pod"], "container": p["container"]},
                     "value": [at, str(p["restarts"])]} for p in self.pods if p["restarts"]]
        return self._node_query(promql, at)

    def _node_query(self, promql, at):
        def vec(metric, value):
            return {"metric": metric, "value": [at, repr(float(value))]}

        pods = [p for p in self.pods if p["node"]]
        if "kube_node_info" in promql:
            return [vec({"node": n["name"]}, 1) for n in self.nodes]
        if "kube_node_role" in promql:
            return [vec({"node": n["name"], "role": n["role"]}, 1) for n in self.nodes]
        if "kube_node_status_allocatable" in promql:
            out = []
            for n in self.nodes:
                out.append(vec({"node": n["name"], "resource": "cpu"}, n["cpu"]))
                out.append(vec({"node": n["name"], "resource": "memory"}, n["mem"]))
            return out
        if "kube_node_status_condition" in promql:
            return [vec({"node": n["name"], "status": "true"}, 1) for n in self.nodes if n["ready"]]
        if "kube_node_spec_unschedulable" in promql:
            return [vec({"node": n["name"]}, 1) for n in self.nodes if n["unschedulable"]]
        if "kube_pod_info" in promql:
            return [vec({"namespace": p["ns"], "pod": p["pod"], "node": p["node"]}, 1) for p in pods]
        out = []
        for p in pods:
            base = {"node": p["node"], "namespace": p["ns"], "pod": p["pod"], "container": p["container"]}
            if "kube_pod_container_resource_requests" in promql:
                if p["req_cpu"] is not None:
                    out.append(vec({**base, "resource": "cpu"}, p["req_cpu"]))
                if p["req_mem"] is not None:
                    out.append(vec({**base, "resource": "memory"}, p["req_mem"]))
            elif "kube_pod_container_resource_limits" in promql:
                if p["lim_cpu"] is not None:
                    out.append(vec({**base, "resource": "cpu"}, p["lim_cpu"]))
                if p["lim_mem"] is not None:
                    out.append(vec({**base, "resource": "memory"}, p["lim_mem"]))
        return out

    def runtime_info(self):
        return {"storageRetention": self.retention}

    def ping(self):
        if self.fail:
            raise PrometheusError("Prometheus indisponível (simulado)")


class FakeKubeApi:
    """API do Kubernetes simulada: registra as chamadas, guarda Secrets e responde 403 para caminhos marcados."""

    def __init__(self):
        self.calls = []
        self.forbidden = set()
        self.secrets = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                outer.calls.append({"method": method, "path": self.path,
                                    "type": self.headers.get("Content-Type"), "body": body})
                status, payload = 200, {}
                if any(p in self.path for p in outer.forbidden):
                    status, payload = 403, {"message": "forbidden: sem permissão"}
                elif "/secrets" in self.path and method == "GET":
                    name = self.path.rsplit("/", 1)[-1]
                    if name in outer.secrets:
                        payload = outer.secrets[name]
                    else:
                        status, payload = 404, {"message": "secret não encontrado"}
                elif "/secrets" in self.path and method == "POST":
                    name = body["metadata"]["name"]
                    outer.secrets[name] = {"metadata": {"name": name}, "data": {
                        k: base64.b64encode(v.encode()).decode()
                        for k, v in body.get("stringData", {}).items()}}
                    status, payload = 201, outer.secrets[name]
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
