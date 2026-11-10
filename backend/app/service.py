"""Cache do snapshot com janela de reutilização e resposta degradada quando o Prometheus falha."""
import threading
import time

from . import formula
from .analysis import NotFound, analyze, build_charts, iso
from .apply import apply as apply_changes
from .nodes import build_node_detail, build_nodes, collect_nodes
from .prometheus import PrometheusClient, PrometheusError, collect

RETRY_SECONDS = 30  # após falha com dado anterior, nova tentativa em 30 s


class AnalysisService:
    def __init__(self, settings, client=None, clock=time.time, kube=None):
        self.settings = settings
        self.kube = kube  # cliente da API do Kubernetes; criado sob demanda (só dentro do cluster)
        self.client = client or PrometheusClient(
            settings.prometheus_url,
            timeout=settings.prometheus_timeout,
            max_concurrency=settings.prometheus_max_concurrency,
            bearer_token=settings.prometheus_bearer_token,
            username=settings.prometheus_username,
            password=settings.prometheus_password,
        )
        self._clock = clock
        self._lock = threading.Lock()  # uma análise por vez; as demais esperam o resultado
        self._formula_lock = threading.Lock()  # uma edição de fórmula por vez
        self._entry = None             # (snapshot, collected, fetched_at)
        self._expires = 0.0
        self._last_error = None
        self._nodes_lock = threading.Lock()
        self._nodes_entry = None       # (consultas brutas de nodes, fetched_at)
        self._nodes_expires = 0.0
        self._nodes_error = None

    def snapshot(self, force=False):
        with self._lock:
            now = self._clock()
            if force or now >= self._expires:
                try:
                    col = collect(self.client, self.settings, now)
                    self._entry = (analyze(col, self.settings, now), col, now)
                    self._last_error = None
                    self._expires = now + self.settings.cache_ttl
                except PrometheusError as exc:
                    self._last_error = str(exc)
                    if self._entry is None:
                        raise
                    self._expires = now + RETRY_SECONDS
            snap, col, fetched_at = self._entry
            meta = {
                "generatedAt": snap["generatedAt"],
                "lastSuccessfulAt": iso(fetched_at),
                "stale": self._last_error is not None,
                "error": self._last_error,
            }
            return snap, col, meta

    def ready(self):
        try:
            self.client.ping()
            return True, None
        except PrometheusError as exc:
            # com um resultado anterior, o serviço continua respondendo, marcado como desatualizado
            return self._entry is not None, str(exc)

    def formula_view(self):
        return formula.describe(self.settings)

    def update_formula(self, payload):
        """Valida, grava e aplica a nova fórmula, e recalcula. Falha do Prometheus propaga após a gravação."""
        with self._formula_lock:
            values = formula.validate(payload, formula.current(self.settings))
            new = formula.apply(self.settings, values)
            if new.formula_file:
                formula.save(new)
            with self._lock:
                self.settings = new
                self._entry = None  # o resultado anterior usa a fórmula antiga; sem ele, não há resposta degradada
                self._expires = 0.0
        return self.snapshot()

    def apply_to_workload(self, namespace, kind, name, container, target):
        _, workload, _ = self.workload(namespace, kind, name)
        return apply_changes(self.kube, workload, container, target)

    def _nodes_raw(self, force=False):
        """Consultas de nodes com o mesmo cache e a mesma resposta degradada dos workloads."""
        with self._nodes_lock:
            now = self._clock()
            if force or now >= self._nodes_expires or self._nodes_entry is None:
                try:
                    self._nodes_entry = (collect_nodes(self.client, now), now)
                    self._nodes_error = None
                    self._nodes_expires = now + self.settings.cache_ttl
                except PrometheusError as exc:
                    self._nodes_error = str(exc)
                    if self._nodes_entry is None:
                        raise
                    self._nodes_expires = now + RETRY_SECONDS
            raw, fetched_at = self._nodes_entry
            meta = {"generatedAt": iso(self._clock()), "lastSuccessfulAt": iso(fetched_at),
                    "stale": self._nodes_error is not None, "error": self._nodes_error}
            return raw, meta

    def nodes(self):
        raw, meta = self._nodes_raw()
        view = build_nodes(raw, self.settings.excluded_namespaces, self._clock())
        return meta, view

    def node_detail(self, name):
        raw, meta = self._nodes_raw()
        detail = build_node_detail(raw, self.settings.excluded_namespaces, name)
        if detail is None:
            raise NotFound(f"node {name} não encontrado")
        return meta, detail

    def workload(self, namespace, kind, name):
        snap, col, meta = self.snapshot()
        found = next((w for w in snap["workloads"]
                      if (w["namespace"], w["kind"], w["name"]) == (namespace, kind, name)), None)
        if found is None:
            raise NotFound(f"workload {kind}/{name} não encontrado no namespace {namespace}")
        return meta, found, build_charts(col, namespace, kind, name)
