"""API HTTP (biblioteca padrão). Rotas em /api/*."""
import base64
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .analysis import (
    ALL_STATUSES, SORT_KEYS, NotFound, export_csv, filter_workloads, namespace_summaries,
    namespace_totals,
)
from . import admin, formula
from .apply import ApplyError
from .cluster import KubeClient, KubeError
from .config import Settings
from .formula import FormulaError
from .prometheus import PrometheusError
from .service import AnalysisService

NAMESPACE_RE = re.compile(r"^/api/namespaces/([^/]+)$")
EXPORT_RE = re.compile(r"^/api/namespaces/([^/]+)/export\.csv$")
WORKLOAD_RE = re.compile(r"^/api/namespaces/([^/]+)/workloads/([^/]+)/([^/]+)$")
APPLY_RE = re.compile(r"^/api/namespaces/([^/]+)/workloads/([^/]+)/([^/]+)/apply$")
NODE_RE = re.compile(r"^/api/nodes/([^/]+)$")
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

    def do_PUT(self):
        self._route("PUT")

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
            if method == "PUT" and path == "/api/formula":
                return self._update_formula(service)
            match = APPLY_RE.match(path) if method == "POST" else None
            if match:
                return self._apply(service, *(unquote(g) for g in match.groups()))
            if method != "GET":
                raise ApiError(405, "método não permitido")

            if path == "/api/formula":
                return self._json(200, {"formula": service.formula_view(),
                                        "editable": bool(service.settings.admin_password),
                                        "adminUser": service.settings.admin_user,
                                        "persisted": bool(service.settings.formula_file)})

            if path == "/api/overview":
                snap, _, meta = service.snapshot()
                return self._json(200, {"meta": meta, "window": snap["window"],
                                        "quality": snap["quality"],
                                        "namespaces": namespace_summaries(snap)})
            if path == "/api/namespaces":
                snap, _, meta = service.snapshot()
                return self._json(200, {"meta": meta, "namespaces": namespace_summaries(snap)})

            if path == "/api/nodes":
                meta, view = service.nodes()
                return self._json(200, {"meta": meta, **view})

            match = NODE_RE.match(path)
            if match:
                meta, detail = service.node_detail(match.group(1))
                return self._json(200, {"meta": meta, **detail})

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
                writes = bool(service.settings.allow_cluster_changes and service.settings.admin_password)
                return self._json(200, {"meta": meta, "workload": workload, "charts": charts, "writes": writes,
                                        "adminUser": service.settings.admin_user})

            raise ApiError(404, "rota inexistente")
        except PrometheusError as exc:
            return self._json(503, {"error": "Prometheus indisponível", "detail": str(exc)})
        except NotFound as exc:
            return self._json(404, {"error": str(exc)})
        except FormulaError as exc:
            return self._json(400, {"error": "fórmula inválida", "fields": exc.fields})
        except ApiError as exc:
            return self._json(exc.status, {"error": str(exc)})
        except (ApplyError, KubeError) as exc:
            return self._json(exc.status, {"error": str(exc)})

    def _apply(self, service, namespace, kind, name):
        settings = service.settings
        if not settings.allow_cluster_changes:
            raise ApiError(403, "alterações no cluster desabilitadas: defina ALLOW_CLUSTER_CHANGES=true")
        self._require_admin(settings)
        body = self._read_json()
        if not isinstance(body, dict) or not isinstance(body.get("container"), str) or not body["container"]:
            raise ApiError(400, "informe o container e o target (workload ou pods)")
        result = service.apply_to_workload(namespace, kind, name, body["container"], body.get("target"))
        return self._json(200, result)

    def _update_formula(self, service):
        self._require_admin(service.settings)
        payload = self._read_json()
        try:
            snap, _, meta = service.update_formula(payload)
        except PrometheusError as exc:
            # a fórmula já foi gravada e aplicada; só o recálculo falhou
            return self._json(503, {"error": "Fórmula salva, mas o recálculo falhou",
                                    "detail": str(exc), "formula": service.formula_view()})
        except OSError as exc:
            return self._json(500, {"error": "não foi possível gravar a fórmula", "detail": str(exc)})
        return self._json(200, {"formula": service.formula_view(), "meta": meta,
                                "generatedAt": snap["generatedAt"]})

    def _require_admin(self, settings):
        if not settings.admin_password:
            raise ApiError(403, "edição desabilitada: credenciais de administrador indisponíveis")
        user, password = "", ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                pass
        ok_user = hmac.compare_digest(user.encode("utf-8"), settings.admin_user.encode("utf-8"))
        ok_password = hmac.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        if not (ok_user and ok_password):
            raise ApiError(401, "usuário ou senha de administrador inválidos")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length inválido")
        if not 0 < length <= formula.MAX_BODY_BYTES:
            raise ApiError(400, "corpo ausente ou maior que o permitido")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "JSON inválido")

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
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST"))
    settings = admin.load_or_create(formula.restore(settings), kube=KubeClient() if in_cluster else None)
    server = make_server(AnalysisService(settings), port=int(os.environ.get("PORT", "8080")))
    print(f"resource-advisor em http://0.0.0.0:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
