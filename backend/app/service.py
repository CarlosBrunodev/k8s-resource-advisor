"""Cache do snapshot com janela de reutilização e resposta degradada quando o Prometheus falha."""
import threading
import time

from .analysis import NotFound, analyze, build_charts, iso
from .prometheus import PrometheusClient, PrometheusError, collect

RETRY_SECONDS = 30  # após falha com dado anterior, nova tentativa em 30 s


class AnalysisService:
    def __init__(self, settings, client=None, clock=time.time):
        self.settings = settings
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
        self._entry = None             # (snapshot, collected, fetched_at)
        self._expires = 0.0
        self._last_error = None

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

    def workload(self, namespace, kind, name):
        snap, col, meta = self.snapshot()
        found = next((w for w in snap["workloads"]
                      if (w["namespace"], w["kind"], w["name"]) == (namespace, kind, name)), None)
        if found is None:
            raise NotFound(f"workload {kind}/{name} não encontrado no namespace {namespace}")
        return meta, found, build_charts(col, namespace, kind, name)
