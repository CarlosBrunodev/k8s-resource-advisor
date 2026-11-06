import json
import threading
import unittest
import urllib.error
import urllib.request

from app.analysis import MIB
from app.server import make_server
from app.service import AnalysisService
from tests.fakes import FakeProm, make_settings

NOW = 1_700_000_000.0


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeProm()
        cls.fake.add_rs("shop", "api-7d9f8b6c5d", "api")
        cls.fake.add_pod("shop", "api-7d9f8b6c5d-x1", rs="api-7d9f8b6c5d", cpu=0.2, mem=300 * MIB,
                         req_cpu=0.1, req_mem=256 * MIB, lim_cpu=0.5, lim_mem=512 * MIB)
        cls.clock = [NOW]
        cls.service = AnalysisService(make_settings(), client=cls.fake, clock=lambda: cls.clock[0])
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def get_json(self, path):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def test_health_probes(self):
        self.assertEqual(self.get_json("/api/health/live")[0], 200)
        self.assertEqual(self.get_json("/api/health/ready")[0], 200)

    def test_overview_and_namespace_listing(self):
        status, body = self.get_json("/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(body["window"]["days"], 7)
        shop = next(n for n in body["namespaces"] if n["name"] == "shop")
        self.assertEqual(shop["counts"]["desvio"], 1)
        self.assertFalse(body["meta"]["stale"])

    def test_namespace_filters_and_validation(self):
        status, body = self.get_json("/api/namespaces/shop?status=desvio&sort=ajuste")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["workloads"]), 1)
        self.assertEqual(body["totals"]["counts"]["desvio"], 1)
        status, body = self.get_json("/api/namespaces/shop?status=adequado")
        self.assertEqual(body["workloads"], [])
        self.assertEqual(self.get_json("/api/namespaces/shop?status=nope")[0], 400)
        self.assertEqual(self.get_json("/api/namespaces/shop?resource=disk")[0], 400)
        self.assertEqual(self.get_json("/api/namespaces/ausente")[0], 404)

    def test_workload_detail_and_missing_workload(self):
        status, body = self.get_json("/api/namespaces/shop/workloads/Deployment/api")
        self.assertEqual(status, 200)
        self.assertEqual(body["workload"]["name"], "api")
        self.assertTrue(body["charts"][0]["cpu"]["consumption"])
        self.assertIn("requests:", body["workload"]["containers"][0]["yaml"])
        self.assertEqual(self.get_json("/api/namespaces/shop/workloads/Deployment/nope")[0], 404)

    def test_csv_export_has_bom_and_header(self):
        status, headers, body = self.get("/api/namespaces/shop/export.csv?status=desvio")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/csv"))
        text = body.decode("utf-8-sig")
        self.assertTrue(text.startswith("Workload,Tipo de workload,"))
        self.assertIn("recomendacoes-shop-v0.csv", headers["Content-Disposition"])
        self.assertEqual(text.count("\n"), 5)  # cabeçalho + 4 itens

    def test_prometheus_failure_serves_stale_snapshot_then_503_without_history(self):
        self.clock[0] = NOW + 400  # passou do TTL
        self.fake.fail = True
        try:
            status, body = self.get_json("/api/overview")
            self.assertEqual(status, 200)
            self.assertTrue(body["meta"]["stale"])
            self.assertIn("indisponível", body["meta"]["error"])
            self.assertEqual(self.get_json("/api/health/ready")[0], 200)

            fresh = AnalysisService(make_settings(), client=self.fake, clock=lambda: NOW)
            fresh_server = make_server(fresh, "127.0.0.1", 0)
            threading.Thread(target=fresh_server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{fresh_server.server_address[1]}/api/overview"
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url, timeout=10)
                self.assertEqual(ctx.exception.code, 503)
                ready_url = url.replace("/api/overview", "/api/health/ready")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(ready_url, timeout=10)
                self.assertEqual(ctx.exception.code, 503)
            finally:
                fresh_server.shutdown()
                fresh_server.server_close()
        finally:
            self.fake.fail = False
            self.clock[0] = NOW


if __name__ == "__main__":
    unittest.main()
