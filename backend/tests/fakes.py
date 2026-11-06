"""Prometheus simulado e configuração de teste. Responde às mesmas consultas usadas em produção."""
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
        excluded_namespaces=("kube-system",), cpu_floor_m=10, memory_floor_mi=32,
    )
    base.update(overrides)
    return Settings(**base)


class FakeProm:
    max_concurrency = 2

    def __init__(self, retention="15d"):
        self.pods = []
        self.rs_owners = []
        self.retention = retention
        self.fail = False

    def add_pod(self, ns, pod, container="app", *, deployment="api", rs=None, cpu=0.0, mem=0.0,
                req_cpu=None, req_mem=None, lim_cpu=None, lim_mem=None,
                first=None, last=None, oom=0, throttle=0.0, restarts=0):
        """cpu em cores, mem/req_mem/lim_mem em bytes; first/last delimitam a vida do pod (None = sem limite)."""
        self.pods.append(dict(ns=ns, pod=pod, container=container, deployment=deployment, rs=rs,
                              cpu=cpu, mem=mem, req_cpu=req_cpu, req_mem=req_mem, lim_cpu=lim_cpu,
                              lim_mem=lim_mem, first=first, last=last, oom=oom, throttle=throttle,
                              restarts=restarts))

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
        return [{"metric": {"namespace": p["ns"], "pod": p["pod"], "container": p["container"]},
                 "value": [at, str(p["restarts"])]} for p in self.pods if p["restarts"]]

    def runtime_info(self):
        return {"storageRetention": self.retention}

    def ping(self):
        if self.fail:
            raise PrometheusError("Prometheus indisponível (simulado)")
