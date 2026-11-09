"""Fórmula editável: janela, percentis e margens da recomendação.

A API trabalha em percentuais (como as variáveis de ambiente). Settings guarda as margens e a tolerância como fração.
Edições são validadas, aplicadas ao Settings em uso e gravadas em FORMULA_FILE (JSON), se configurado.
"""
import json
import math
import os
import tempfile
from dataclasses import replace

from .analysis import method_text

# nome na API: (campo de Settings, tipo, mínimo, máximo, valor em percentual?)
FIELDS = {
    "window_days": ("window_days", int, 1, 30, False),
    "reference_percentile": ("reference_percentile", float, 1, 100, False),
    "cpu_limit_percentile": ("cpu_limit_percentile", float, 1, 100, False),
    "memory_limit_percentile": ("memory_limit_percentile", float, 1, 100, False),
    "request_margin_percent": ("request_margin", float, 0, 500, True),
    "limit_margin_percent": ("limit_margin", float, 0, 500, True),
    "tolerance_percent": ("tolerance", float, 0, 100, True),
}
MAX_BODY_BYTES = 16 * 1024


class FormulaError(Exception):
    """Payload inválido. `fields` mapeia cada campo para a mensagem de erro."""

    def __init__(self, fields):
        super().__init__("fórmula inválida")
        self.fields = fields


def current(s):
    """Valores atuais em unidades da API."""
    values = {}
    for name, (attr, _, _, _, percent) in FIELDS.items():
        value = getattr(s, attr)
        values[name] = round(value * 100, 6) if percent else value
    return values


def validate(payload, base):
    """Mescla o payload (pode ser parcial) sobre `base` e devolve todos os valores validados."""
    if not isinstance(payload, dict):
        raise FormulaError({"_": "envie um objeto JSON"})
    values = dict(base)
    errors = {}
    for name, raw in payload.items():
        if name not in FIELDS:
            errors[name] = "campo desconhecido"
            continue
        _, kind, lo, hi, _ = FIELDS[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            errors[name] = "informe um número"
        elif kind is int and not float(raw).is_integer():
            errors[name] = "informe um número inteiro"
        elif not lo <= raw <= hi:
            errors[name] = f"deve estar entre {lo} e {hi}"
        else:
            values[name] = kind(raw)
    if errors:
        raise FormulaError(errors)
    return values


def apply(s, values):
    """Settings novo com os valores informados (a instância anterior não é alterada)."""
    changes = {}
    for name, (attr, _, _, _, percent) in FIELDS.items():
        changes[attr] = values[name] / 100 if percent else values[name]
    return replace(s, **changes)


def restore(s):
    """Aplica a fórmula gravada em FORMULA_FILE, se existir. Arquivo inválido interrompe a inicialização."""
    if not s.formula_file or not os.path.exists(s.formula_file):
        return s
    with open(s.formula_file, encoding="utf-8") as fh:
        stored = json.load(fh)
    return apply(s, validate(stored, current(s)))


def save(s):
    """Grava a fórmula atual de forma atômica: arquivo temporário e troca no mesmo diretório."""
    directory = os.path.dirname(os.path.abspath(s.formula_file))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".formula-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(current(s), fh, indent=2, ensure_ascii=False)
    os.replace(tmp, s.formula_file)


def describe(s):
    """Valores editáveis e os textos de método exibidos nos resultados (mesma origem do campo `method`)."""
    days = s.window_days
    return {
        "values": current(s),
        "texts": {
            "request": method_text("cpu", "request", s, days),
            "cpuLimit": method_text("cpu", "limit", s, days),
            "memoryLimit": method_text("memory", "limit", s, days),
        },
    }
