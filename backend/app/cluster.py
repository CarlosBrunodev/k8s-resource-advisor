"""Cliente mínimo da API do Kubernetes (biblioteca padrão). Usado para ler e criar o Secret do administrador
e para os ajustes aprovados na tela."""
import json
import os
import ssl
import urllib.error
import urllib.request
from urllib.parse import quote

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
STRATEGIC_MERGE = "application/strategic-merge-patch+json"
JSON = "application/json"
WORKLOAD_RESOURCES = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}


class KubeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _q(value):
    return quote(value, safe="")


def workload_path(kind, namespace, name):
    return f"/apis/apps/v1/namespaces/{_q(namespace)}/{WORKLOAD_RESOURCES[kind]}/{_q(name)}"


def resize_path(namespace, pod):
    return f"/api/v1/namespaces/{_q(namespace)}/pods/{_q(pod)}/resize"


def secrets_path(namespace, name=None):
    base = f"/api/v1/namespaces/{_q(namespace)}/secrets"
    return base if name is None else f"{base}/{_q(name)}"


class KubeClient:
    def __init__(self, base_url=None, token_file=None, ca_file=None, namespace=None, timeout=15):
        if base_url is None:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            if not host:
                raise KubeError(503, "API do Kubernetes indisponível: o back-end não está rodando dentro do cluster")
            base_url = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file or os.path.join(SA_DIR, "token")
        self.ca_file = ca_file or os.path.join(SA_DIR, "ca.crt")
        self._namespace = namespace
        self.timeout = timeout

    def namespace(self):
        """Namespace onde o próprio back-end roda."""
        if self._namespace is None:
            with open(os.path.join(SA_DIR, "namespace"), encoding="utf-8") as fh:
                self._namespace = fh.read().strip()
        return self._namespace

    def request(self, method, path, body=None, content_type=JSON):
        """Chamada à API. Erro da API vira KubeError com a mensagem do Kubernetes."""
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        if os.path.exists(self.token_file):  # relido a cada chamada: o token pode ser rotacionado
            with open(self.token_file, encoding="utf-8") as fh:
                headers["Authorization"] = f"Bearer {fh.read().strip()}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        context = ssl.create_default_context(cafile=self.ca_file) if self.base_url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=context) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw).get("message") or raw
            except (ValueError, AttributeError):
                message = raw
            raise KubeError(exc.code, message[:300] or f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            raise KubeError(502, f"API do Kubernetes inacessível: {exc}")

    def get(self, path):
        return self.request("GET", path)

    def create(self, path, body):
        return self.request("POST", path, body)

    def patch(self, path, body):
        return self.request("PATCH", path, body, STRATEGIC_MERGE)
