"""Aplicação das sugestões no cluster.

O plano sai sempre da análise atual do container, nunca do corpo da requisição: a tela só escolhe o container e o alvo.
- alvo "workload": patch no Deployment, StatefulSet ou DaemonSet (persistente, com rolling update);
- alvo "pods": resize in-place de cada réplica ativa (subresource pods/resize; não persiste quando o pod é recriado).
"""
from .cluster import WORKLOAD_RESOURCES, KubeClient, KubeError, resize_path, workload_path

TARGETS = ("workload", "pods")


class ApplyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _fmt(resource, value):
    return f"{round(value)}m" if resource == "cpu" else f"{round(value)}Mi"


def plan(container):
    """Mudanças de requests e limits de CPU e memória que têm sugestão, no formato da API."""
    changes, requests, limits = [], {}, {}
    for item in container["items"]:
        if item["suggested"] is None:
            continue
        resource = "cpu" if item["resource"] == "cpu" else "memory"
        field = "requests" if item["kind"] == "request" else "limits"
        (requests if field == "requests" else limits)[resource] = _fmt(item["resource"], item["suggested"])
        changes.append({
            "resource": resource,
            "field": field,
            "from": None if item["current"] is None else _fmt(item["resource"], item["current"]),
            "to": _fmt(item["resource"], item["suggested"]),
        })
    if not changes:
        raise ApplyError(409, "nenhuma sugestão para aplicar neste container")
    resources = {}
    if requests:
        resources["requests"] = requests
    if limits:
        resources["limits"] = limits
    return {"changes": changes, "resources": resources}


def _container_body(name, resources):
    return {"name": name, "resources": resources}


def apply(kube, workload, container_name, target):
    """Executa o alvo pedido e devolve as mudanças e o resultado de cada chamada à API."""
    if target not in TARGETS:
        raise ApplyError(400, "target deve ser workload ou pods")
    container = next((c for c in workload["containers"] if c["name"] == container_name), None)
    if container is None:
        raise ApplyError(404, f"container {container_name} não encontrado no workload")
    planned = plan(container)
    namespace, kind, name = workload["namespace"], workload["kind"], workload["name"]
    client = kube or KubeClient()

    if target == "workload":
        if kind not in WORKLOAD_RESOURCES:
            raise ApplyError(422, f"{kind} não é aplicado pelo app; use Deployment, StatefulSet ou DaemonSet")
        body = {"spec": {"template": {"spec": {"containers": [_container_body(container_name, planned["resources"])]}}}}
        try:
            client.patch(workload_path(kind, namespace, name), body)
        except KubeError as exc:
            raise ApplyError(502, f"Kubernetes recusou o patch em {kind}/{name}: {exc}")
        results = [{"kind": kind, "name": name, "status": "ok"}]
    else:
        pods = [p["pod"] for p in container["pods"] if p["active"]]
        if not pods:
            raise ApplyError(409, "nenhum pod ativo para aplicar o resize")
        body = {"spec": {"containers": [_container_body(container_name, planned["resources"])]}}
        results = []
        for pod in pods:
            try:
                client.patch(resize_path(namespace, pod), body)
                results.append({"pod": pod, "status": "ok"})
            except KubeError as exc:
                results.append({"pod": pod, "status": "erro", "detail": str(exc)})
    return {"target": target, "container": container_name, "changes": planned["changes"], "results": results}
