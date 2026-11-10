"""Credenciais do administrador (usuário e senha), geradas no primeiro boot.

- Dentro do cluster: ficam no Secret ADMIN_SECRET (chaves username e password), no namespace do back-end.
  Se o Secret já existir, é reutilizado; senão, a senha é gerada e o Secret é criado.
- Fora do cluster (ex.: Docker local): ficam no arquivo ADMIN_FILE (JSON, permissão 0600).

Para trocar a senha, apague o Secret (ou o arquivo) e reinicie o back-end. A senha nunca vai para o log.
"""
import base64
import json
import os
import secrets
import tempfile
from dataclasses import replace

from .cluster import KubeError, secrets_path


def _generate():
    return secrets.token_urlsafe(24)


def load_or_create(settings, kube=None):
    """Devolve o Settings com admin_user e admin_password preenchidos, quando possível."""
    if kube is not None:
        try:
            return _from_secret(settings, kube)
        except (KubeError, OSError) as exc:
            print(f"credenciais do administrador indisponíveis: {exc}", flush=True)
            return settings
    if settings.admin_file:
        try:
            return _from_file(settings)
        except OSError as exc:
            print(f"não foi possível usar ADMIN_FILE: {exc}", flush=True)
            return settings
    print("edição da fórmula e aplicação no cluster desabilitadas: sem Secret nem ADMIN_FILE", flush=True)
    return settings


def _from_secret(settings, kube):
    namespace = kube.namespace()
    path = secrets_path(namespace, settings.admin_secret)
    try:
        secret = kube.get(path)
    except KubeError as exc:
        if exc.status != 404:
            raise
        password = _generate()
        kube.create(secrets_path(namespace), {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": settings.admin_secret,
                         "labels": {"app.kubernetes.io/name": "resource-advisor",
                                    "app.kubernetes.io/component": "backend"}},
            "type": "Opaque",
            "stringData": {"username": settings.admin_user, "password": password},
        })
        print(f"Secret {settings.admin_secret} criado com as credenciais do administrador", flush=True)
        return replace(settings, admin_password=password)
    data = secret.get("data") or {}
    if "username" not in data or "password" not in data:
        raise KubeError(500, f"Secret {settings.admin_secret} sem as chaves username e password")
    return replace(settings,
                   admin_user=base64.b64decode(data["username"]).decode("utf-8"),
                   admin_password=base64.b64decode(data["password"]).decode("utf-8"))


def _from_file(settings):
    if os.path.exists(settings.admin_file):
        with open(settings.admin_file, encoding="utf-8") as fh:
            stored = json.load(fh)
        return replace(settings, admin_user=stored["username"], admin_password=stored["password"])
    password = _generate()
    directory = os.path.dirname(os.path.abspath(settings.admin_file))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".admin-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"username": settings.admin_user, "password": password}, fh)
    os.replace(tmp, settings.admin_file)
    print(f"credenciais do administrador gravadas em {settings.admin_file}", flush=True)
    return replace(settings, admin_password=password)
