import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace

from app import formula
from app.analysis import MIB
from app.formula import FormulaError
from app.server import make_server
from app.service import AnalysisService
from tests.fakes import FakeProm, make_settings

NOW = 1_700_000_000.0
TOKEN = "segredo-de-teste"


def _request(base, path, method="GET", body=None, token=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class FormulaUnitTests(unittest.TestCase):
    def test_current_reports_percentages_and_validate_merges_partial_payload(self):
        s = make_settings(request_margin=0.2, tolerance=0.1)
        base = formula.current(s)
        self.assertEqual(base["request_margin_percent"], 20.0)
        self.assertEqual(base["tolerance_percent"], 10.0)
        values = formula.validate({"reference_percentile": 90}, base)
        self.assertEqual(values["reference_percentile"], 90.0)
        self.assertEqual(values["request_margin_percent"], 20.0)

    def test_apply_converts_margins_back_to_fraction(self):
        s = formula.apply(make_settings(), {**formula.current(make_settings()), "request_margin_percent": 35})
        self.assertAlmostEqual(s.request_margin, 0.35)

    def test_validate_rejects_bad_values_with_field_errors(self):
        base = formula.current(make_settings())
        with self.assertRaises(FormulaError) as ctx:
            formula.validate({"window_days": 0, "reference_percentile": "95", "window_days_x": 1,
                              "tolerance_percent": True, "limit_margin_percent": float("nan")}, base)
        fields = ctx.exception.fields
        self.assertIn("entre 1 e 30", fields["window_days"])
        self.assertEqual(fields["reference_percentile"], "informe um número")
        self.assertEqual(fields["window_days_x"], "campo desconhecido")
        self.assertEqual(fields["tolerance_percent"], "informe um número")
        self.assertEqual(fields["limit_margin_percent"], "informe um número")

    def test_validate_rejects_fractional_window(self):
        with self.assertRaises(FormulaError) as ctx:
            formula.validate({"window_days": 7.5}, formula.current(make_settings()))
        self.assertEqual(ctx.exception.fields["window_days"], "informe um número inteiro")

    def test_save_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "formula.json")
            s = replace(make_settings(), formula_file=path)
            s = formula.apply(s, {**formula.current(s), "cpu_limit_percentile": 98.0, "request_margin_percent": 30})
            formula.save(s)
            restored = formula.restore(replace(make_settings(), formula_file=path))
            self.assertEqual(restored.cpu_limit_percentile, 98.0)
            self.assertAlmostEqual(restored.request_margin, 0.30)
            self.assertEqual(restored.reference_percentile, 95.0)

    def test_restore_without_file_keeps_settings(self):
        s = make_settings(formula_file=None)
        self.assertIs(formula.restore(s), s)


class FormulaApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeProm()
        cls.fake.add_rs("shop", "api-7d9f8b6c5d", "api")
        cls.fake.add_pod("shop", "api-7d9f8b6c5d-x1", rs="api-7d9f8b6c5d", cpu=0.2, mem=300 * MIB,
                         req_cpu=0.1, req_mem=256 * MIB, lim_cpu=0.5, lim_mem=512 * MIB)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "formula.json")
        settings = replace(make_settings(), admin_token=TOKEN, formula_file=cls.path)
        cls.service = AnalysisService(settings, client=cls.fake, clock=lambda: NOW)
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def test_get_formula_is_public_and_shows_method_text(self):
        status, body = _request(self.base, "/api/formula")
        self.assertEqual(status, 200)
        self.assertTrue(body["editable"])
        self.assertTrue(body["persisted"])
        self.assertEqual(body["formula"]["texts"]["request"], "7 d · P95 · margem 20%")
        self.assertEqual(body["formula"]["texts"]["memoryLimit"], "7 d · máximo · margem 20%")

    def test_put_requires_valid_token(self):
        status, body = _request(self.base, "/api/formula", "PUT", {"reference_percentile": 90})
        self.assertEqual(status, 401)
        status, body = _request(self.base, "/api/formula", "PUT", {"reference_percentile": 90}, token="errado")
        self.assertEqual(status, 401)
        self.assertEqual(self.service.settings.reference_percentile, 95.0)

    def test_put_disabled_without_admin_token(self):
        other = AnalysisService(make_settings(), client=self.fake, clock=lambda: NOW)
        server = make_server(other, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            status, body = _request(base, "/api/formula", "PUT", {"reference_percentile": 90}, token="x")
            self.assertEqual(status, 403)
            self.assertIn("ADMIN_TOKEN", body["error"])
        finally:
            server.shutdown()
            server.server_close()

    def test_put_validation_error_returns_400_and_changes_nothing(self):
        status, body = _request(self.base, "/api/formula", "PUT", {"window_days": 99}, token=TOKEN)
        self.assertEqual(status, 400)
        self.assertIn("window_days", body["fields"])
        self.assertEqual(self.service.settings.window_days, 7)

    def test_put_applies_recalculates_and_persists(self):
        status, body = _request(self.base, "/api/formula", "PUT",
                                {"reference_percentile": 90, "request_margin_percent": 30}, token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["formula"]["texts"]["request"], "7 d · P90 · margem 30%")
        self.assertEqual(self.service.settings.reference_percentile, 90.0)

        # o resultado recalculado usa a nova fórmula (justificativa e método dos itens)
        status, detail = _request(self.base, "/api/namespaces/shop/workloads/Deployment/api")
        self.assertEqual(status, 200)
        request_item = next(i for i in detail["workload"]["items"] if i["kind"] == "request" and i["resource"] == "cpu")
        self.assertEqual(request_item["method"], "7 d · P90 · margem 30%")
        self.assertIn("(1 + 30%)", request_item["justification"])

        with open(self.path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["reference_percentile"], 90.0)
        self.assertEqual(stored["request_margin_percent"], 30.0)

        # restaura a fórmula padrão para não afetar os outros testes deste módulo
        _request(self.base, "/api/formula", "PUT",
                 {"reference_percentile": 95, "request_margin_percent": 20}, token=TOKEN)


if __name__ == "__main__":
    unittest.main()
