"""Cliente HTTP do Prometheus e coleta das séries usadas na análise."""
import base64
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

_RETENTION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


class PrometheusError(Exception):
    """Falha ao consultar o Prometheus."""


def parse_retention(text):
    """Converte storageRetention do Prometheus (ex.: '15d', '720h') em segundos."""
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", text)
    if not match:
        return None
    return float(match.group(1)) * _RETENTION_UNITS[match.group(2)]


class PrometheusClient:
    def __init__(self, base_url, timeout, max_concurrency,
                 bearer_token=None, username=None, password=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_concurrency = max_concurrency
        self._headers = {"Accept": "application/json"}
        if bearer_token:
            self._headers["Authorization"] = f"Bearer {bearer_token}"
        elif username:
            creds = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._headers["Authorization"] = f"Basic {creds}"
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def _get(self, path, params):
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self._headers)
        with self._slots:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.load(response)
            except urllib.error.HTTPError as exc:
                raise PrometheusError(f"Prometheus respondeu HTTP {exc.code} em {path}") from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise PrometheusError(f"falha ao consultar {path}: {exc}") from exc
        if body.get("status") != "success":
            raise PrometheusError(body.get("error") or f"resposta inválida em {path}")
        return body["data"]

    def query_range(self, promql, start, end, step):
        data = self._get("/api/v1/query_range", {
            "query": promql, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step),
        })
        return data["result"]

    def query(self, promql, at):
        return self._get("/api/v1/query", {"query": promql, "time": f"{at:.3f}"})["result"]

    def runtime_info(self):
        return self._get("/api/v1/status/runtimeinfo", {})

    def ping(self):
        self._get("/api/v1/status/buildinfo", {})


@dataclass
class ContainerSeries:
    cpu: list = field(default_factory=list)       # (ts, cores)
    memory: list = field(default_factory=list)    # (ts, bytes)
    throttle: list = field(default_factory=list)  # (ts, razão 0..1)
    requests: dict = field(default_factory=lambda: {"cpu": [], "memory": []})
    limits: dict = field(default_factory=lambda: {"cpu": [], "memory": []})
    oom_buckets: int = 0
    restarts: int = 0


@dataclass
class Collected:
    window_start: float
    window_end: float
    step: int
    retention_seconds: float | None
    containers: dict = field(default_factory=dict)   # (ns, pod, container) -> ContainerSeries
    pod_owner: dict = field(default_factory=dict)    # (ns, pod) -> (kind, name)
    rs_owner: dict = field(default_factory=dict)     # (ns, replicaset) -> (kind, name)

    def container(self, key):
        if key not in self.containers:
            self.containers[key] = ContainerSeries()
        return self.containers[key]


def _auto_step(window_seconds):
    """Passo que mantém ~700 pontos por série, em múltiplos de 5 minutos."""
    return max(300, math.ceil(window_seconds / 700 / 300) * 300)


def _points(item):
    out = []
    for ts, raw in item.get("values", []):
        value = float(raw)
        if math.isfinite(value):
            out.append((float(ts), value))
    return out


def _key(metric):
    return (metric.get("namespace", ""), metric.get("pod", ""), metric.get("container", ""))


def collect(client, settings, now):
    """Executa as consultas do Prometheus e devolve as séries da janela móvel."""
    window_seconds = settings.window_days * 86400
    start, end = now - window_seconds, now
    step = settings.step_seconds or _auto_step(window_seconds)

    base = 'namespace=~".+"'
    if settings.excluded_namespaces:
        base += f',namespace!~"{"|".join(settings.excluded_namespaces)}"'
    ctr = f'{base},container!="",container!="POD",image!=""'

    range_queries = {
        "cpu": f"sum by (namespace,pod,container)(rate(container_cpu_usage_seconds_total{{{ctr}}}[5m]))",
        "memory": (f"max by (namespace,pod,container)"
                   f"(max_over_time(container_memory_working_set_bytes{{{ctr}}}[{step}s]))"),
        "throttle": (f"sum by (namespace,pod,container)(rate(container_cpu_cfs_throttled_periods_total{{{ctr}}}[5m]))"
                     f" / sum by (namespace,pod,container)(rate(container_cpu_cfs_periods_total{{{ctr}}}[5m]))"),
        "requests": (f'max by (namespace,pod,container,resource)'
                     f'(kube_pod_container_resource_requests{{{base},resource=~"cpu|memory"}})'),
        "limits": (f'max by (namespace,pod,container,resource)'
                   f'(kube_pod_container_resource_limits{{{base},resource=~"cpu|memory"}})'),
        "oom": (f'max by (namespace,pod,container)(max_over_time('
                f'kube_pod_container_status_last_terminated_reason{{{base},reason="OOMKilled"}}[{step}s]))'),
        "pod_owner": f"max by (namespace,pod,owner_kind,owner_name)(kube_pod_owner{{{base}}})",
        "rs_owner": f"max by (namespace,replicaset,owner_kind,owner_name)(kube_replicaset_owner{{{base}}})",
    }
    instant_queries = {
        "restarts": (f"sum by (namespace,pod,container)"
                     f"(increase(kube_pod_container_status_restarts_total{{{base}}}[{window_seconds}s]))"),
    }

    with ThreadPoolExecutor(max_workers=client.max_concurrency) as pool:
        range_futures = {k: pool.submit(client.query_range, q, start, end, step)
                         for k, q in range_queries.items()}
        instant_futures = {k: pool.submit(client.query, q, end) for k, q in instant_queries.items()}
        results = {k: f.result() for k, f in range_futures.items()}
        instant = {k: f.result() for k, f in instant_futures.items()}

    try:
        retention = parse_retention(client.runtime_info().get("storageRetention"))
    except PrometheusError:
        retention = None

    col = Collected(window_start=start, window_end=end, step=step, retention_seconds=retention)

    for item in results["cpu"]:
        col.container(_key(item["metric"])).cpu.extend(_points(item))
    for item in results["memory"]:
        col.container(_key(item["metric"])).memory.extend(_points(item))
    for item in results["throttle"]:
        col.container(_key(item["metric"])).throttle.extend(_points(item))
    for item in results["requests"]:
        m = item["metric"]
        col.container(_key(m)).requests[m["resource"]].extend(_points(item))
    for item in results["limits"]:
        m = item["metric"]
        col.container(_key(m)).limits[m["resource"]].extend(_points(item))
    for item in results["oom"]:
        col.container(_key(item["metric"])).oom_buckets = sum(1 for _, v in _points(item) if v > 0)
    for item in instant["restarts"]:
        raw = float(item["value"][1])
        if math.isfinite(raw):
            col.container(_key(item["metric"])).restarts = int(round(raw))

    for item in results["pod_owner"]:
        m = item["metric"]
        if m.get("owner_kind", "<none>") not in ("", "<none>"):
            col.pod_owner[(m["namespace"], m["pod"])] = (m["owner_kind"], m["owner_name"])
    for item in results["rs_owner"]:
        m = item["metric"]
        if m.get("owner_kind", "<none>") not in ("", "<none>"):
            col.rs_owner[(m["namespace"], m["replicaset"])] = (m["owner_kind"], m["owner_name"])
    return col
