"""API HTTP (biblioteca padrão). Rotas em /api/*."""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .analysis import (
    ALL_STATUSES, SORT_KEYS, NotFound, export_csv, filter_workloads, namespace_summaries,
    namespace_totals,
)
from .config import Settings
from .prometheus import PrometheusError
from .service import AnalysisService

NAMESPACE_RE = re.compile(r"^/api/namespaces/([^/]+)$")
EXPORT_RE = re.compile(r"^/api/namespaces/([^/]+)/export\.csv$")
WORKLOAD_RE = re.compile(r"^/api/namespaces/([^/]+)/workloads/([^/]+)/([^/]+)$")
RESOURCES = ("", "cpu", "memory")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _filters(params):
    filters = {
        "q": params.get("q", "").strip(),
        "status": params.get("status", ""),
        "resource": params.get("resource", ""),
        "sort": params.get("sort", "ajuste"),
    }
    if filters["status"] and filters["status"] not in ALL_STATUSES:
        raise ApiError(400, f"status inválido: {filters['status']}")
    if filters["resource"] not in RESOURCES:
        raise ApiError(400, "resource deve ser cpu ou memory")
    if filters["sort"] not in SORT_KEYS:
        raise ApiError(400, "sort deve ser ajuste, pressao ou nome")
    return filters


class Handler(BaseHTTPRequestHandler):
    server_version = "resource-advisor/0.1"

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        url = urlsplit(self.path)
        path = unquote(url.path).rstrip("/") or "/"
        params = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        service = self.server.service
        try:
            if method == "GET" and path == "/api/health/live":
                return self._json(200, {"status": "ok"})
            if method == "GET" and path == "/api/health/ready":
                ready, detail = service.ready()
                return self._json(200 if ready else 503,
                                  {"status": "ready" if ready else "not_ready", "detail": detail})
            if method == "POST" and path == "/api/analysis/refresh":
                snap, _, meta = service.snapshot(force=True)
                return self._json(200, {"meta": meta, "generatedAt": snap["generatedAt"]})
            if method != "GET":
                raise ApiError(405, "método não permitido")

            if path == "/api/overview":
                snap, _, meta = service.snapshot()
                return self._json(200, {"meta": meta, "window": snap["window"],
                                        "quality": snap["quality"],
                                        "namespaces": namespace_summaries(snap)})
            if path == "/api/namespaces":
                snap, _, meta = service.snapshot()
                return self._json(200, {"meta": meta, "namespaces": namespace_summaries(snap)})

            match = NAMESPACE_RE.match(path)
            if match:
                ns = match.group(1)
                filters = _filters(params)
                snap, _, meta = service.snapshot()
                self._require_namespace(snap, ns)
                selected = filter_workloads(snap, ns, **filters)
                return self._json(200, {"meta": meta, "namespace": ns, "window": snap["window"],
                                        "filters": filters, "totals": namespace_totals(selected),
                                        "workloads": selected})

            match = EXPORT_RE.match(path)
            if match:
                ns = match.group(1)
                filters = _filters(params)
                snap, _, _ = service.snapshot()
                self._require_namespace(snap, ns)
                selected = filter_workloads(snap, ns, **filters)
                body = export_csv(selected, snap, ns, filters).encode("utf-8-sig")  # BOM para Excel
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="recomendacoes-{ns}-v0.csv"'})

            match = WORKLOAD_RE.match(path)
            if match:
                meta, workload, charts = service.workload(*match.groups())
                return self._json(200, {"meta": meta, "workload": workload, "charts": charts})

            raise ApiError(404, "rota inexistente")
        except PrometheusError as exc:
            return self._json(503, {"error": "Prometheus indisponível", "detail": str(exc)})
        except NotFound as exc:
            return self._json(404, {"error": str(exc)})
        except ApiError as exc:
            return self._json(exc.status, {"error": str(exc)})

    @staticmethod
    def _require_namespace(snap, ns):
        if not any(w["namespace"] == ns for w in snap["workloads"]):
            raise NotFound(f"namespace {ns} sem workloads analisados")

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def make_server(service, host="0.0.0.0", port=8080):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.service = service
    return server


def main():
    settings = Settings.from_env()
    server = make_server(AnalysisService(settings), port=int(os.environ.get("PORT", "8080")))
    print(f"resource-advisor em http://0.0.0.0:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
