"""Configuração carregada de variáveis de ambiente."""
import os
from dataclasses import dataclass


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    prometheus_bearer_token: str | None
    prometheus_username: str | None
    prometheus_password: str | None
    prometheus_timeout: float
    prometheus_max_concurrency: int
    window_days: int
    step_seconds: int | None
    reference_percentile: float
    cpu_limit_percentile: float
    memory_limit_percentile: float
    request_margin: float
    limit_margin: float
    tolerance: float
    throttling_threshold: float
    min_coverage: float
    min_history_hours: float
    cache_ttl: float
    excluded_namespaces: tuple
    cpu_floor_m: int
    memory_floor_mi: int

    @classmethod
    def from_env(cls):
        window = int(_env("ANALYSIS_WINDOW_DAYS", "7"))
        if not 1 <= window <= 30:
            raise ValueError("ANALYSIS_WINDOW_DAYS deve estar entre 1 e 30")
        url = _env("PROMETHEUS_URL")
        if not url:
            raise ValueError("PROMETHEUS_URL é obrigatória")
        step = _env("PROMETHEUS_STEP_SECONDS")
        excluded = _env("EXCLUDED_NAMESPACES", "kube-system,kube-public,kube-node-lease")
        return cls(
            prometheus_url=url,
            prometheus_bearer_token=_env("PROMETHEUS_BEARER_TOKEN"),
            prometheus_username=_env("PROMETHEUS_USERNAME"),
            prometheus_password=_env("PROMETHEUS_PASSWORD"),
            prometheus_timeout=float(_env("PROMETHEUS_TIMEOUT_SECONDS", "20")),
            prometheus_max_concurrency=int(_env("PROMETHEUS_MAX_CONCURRENCY", "4")),
            window_days=window,
            step_seconds=int(step) if step else None,
            reference_percentile=float(_env("REFERENCE_PERCENTILE", "95")),
            cpu_limit_percentile=float(_env("CPU_LIMIT_PERCENTILE", "99")),
            memory_limit_percentile=float(_env("MEMORY_LIMIT_PERCENTILE", "100")),
            request_margin=float(_env("REQUEST_MARGIN_PERCENT", "20")) / 100,
            limit_margin=float(_env("LIMIT_MARGIN_PERCENT", "20")) / 100,
            tolerance=float(_env("TOLERANCE_PERCENT", "10")) / 100,
            throttling_threshold=float(_env("THROTTLING_THRESHOLD_PERCENT", "5")),
            min_coverage=float(_env("MIN_COVERAGE_PERCENT", "80")) / 100,
            min_history_hours=float(_env("MIN_HISTORY_HOURS", "24")),
            cache_ttl=float(_env("CACHE_TTL_SECONDS", "300")),
            excluded_namespaces=tuple(n.strip() for n in excluded.split(",") if n.strip()),
            cpu_floor_m=int(_env("CPU_FLOOR_MILLICORES", "10")),
            memory_floor_mi=int(_env("MEMORY_FLOOR_MI", "32")),
        )
