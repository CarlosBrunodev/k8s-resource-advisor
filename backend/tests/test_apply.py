import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace

from app import apply as applymod
from app.analysis import MIB
from app.cluster import KubeClient
from app.server import make_server
from app.service import AnalysisService
from tests.fakes import FakeKubeApi, FakeProm, make_settings

NOW = 1_700_000_000.0
USER, PASSWORD = "passuser", "senha-de-teste"
POD = "api-7d9f8b6c5d-x1"


class ApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeProm()
        cls.fake.add_rs("shop", "api-7d9f8b6c5d", "api")
        cls.fake.add_pod("shop", POD, rs="api-7d9f8b6c5d", cpu=0.2, mem=300 * MIB,
                         req_cpu=0.1, req_mem=256 * MIB, lim_cpu=0.5, lim_mem=512 * MIB)
        cls.kube_api = FakeKubeApi()
        kube = KubeClient(base_url=cls.kube_api.url, token_file="/nonexistent", ca_file=None)
        settings = replace(make_settings(), allow_cluster_changes=True, admin_password=PASSWORD)
        cls.service = AnalysisService(settings, client=cls.fake, clock=lambda: NOW, kube=kube)
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.kube_api.close()

    def setUp(self):
        self.kube_api.calls.clear()
        self.kube_api.forbidden.clear()

    def post(self, path, body, password=PASSWORD):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if password is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def suggested(self):
        with urllib.request.urlopen(self.base + "/api/namespaces/shop/workloads/Deployment/api") as resp:
            container = json.loads(resp.read())["workload"]["containers"][0]
        return {(i["kind"], i["resource"]): i["suggested"] for i in container["items"] if i["suggested"] is not None}

    def test_disabled_by_default_returns_403_and_calls_nothing(self):
        other = AnalysisService(make_settings(admin_password=PASSWORD), client=self.fake, clock=lambda: NOW)
        server = make_server(other, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}"
                                         "/api/namespaces/shop/workloads/Deployment/api/apply",
                                         data=b'{"container":"app","target":"workload"}', method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode())
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_requires_admin_password(self):
        status, body = self.post("/api/namespaces/shop/workloads/Deployment/api/apply",
                                 {"container": "app", "target": "workload"}, password="errada")
        self.assertEqual(status, 401)
        self.assertEqual(self.kube_api.calls, [])

    def test_workload_target_patches_deployment_with_suggested_values(self):
        expected = self.suggested()
        status, body = self.post("/api/namespaces/shop/workloads/Deployment/api/apply",
                                 {"container": "app", "target": "workload"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["results"], [{"kind": "Deployment", "name": "api", "status": "ok"}])
        self.assertEqual(len(self.kube_api.calls), 1)
        call = self.kube_api.calls[0]
        self.assertEqual(call["path"], "/apis/apps/v1/namespaces/shop/deployments/api")
        self.assertEqual(call["type"], "application/strategic-merge-patch+json")
        container = call["body"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["name"], "app")
        self.assertEqual(container["resources"]["requests"]["cpu"], f"{expected[('request', 'cpu')]}m")
        self.assertEqual(container["resources"]["requests"]["memory"], f"{expected[('request', 'memory')]}Mi")
        self.assertEqual(container["resources"]["limits"]["cpu"], f"{expected[('limit', 'cpu')]}m")
        self.assertEqual(container["resources"]["limits"]["memory"], f"{expected[('limit', 'memory')]}Mi")
        self.assertTrue(all(c["to"] for c in body["changes"]))

    def test_pods_target_resizes_each_active_replica(self):
        status, body = self.post("/api/namespaces/shop/workloads/Deployment/api/apply",
                                 {"container": "app", "target": "pods"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["results"], [{"pod": POD, "status": "ok"}])
        call = self.kube_api.calls[0]
        self.assertEqual(call["path"], f"/api/v1/namespaces/shop/pods/{POD}/resize")
        self.assertNotIn("template", call["body"]["spec"])
        self.assertEqual(call["body"]["spec"]["containers"][0]["name"], "app")

    def test_pod_failure_is_reported_per_pod_and_workload_failure_is_502(self):
        self.kube_api.forbidden.add(f"/pods/{POD}/resize")
        status, body = self.post("/api/namespaces/shop/workloads/Deployment/api/apply",
                                 {"container": "app", "target": "pods"})
        self.assertEqual(status, 200)
        self.assertEqual(body["results"][0]["status"], "erro")
        self.assertIn("sem permissão", body["results"][0]["detail"])

        self.kube_api.forbidden.add("/deployments/api")
        status, body = self.post("/api/namespaces/shop/workloads/Deployment/api/apply",
                                 {"container": "app", "target": "workload"})
        self.assertEqual(status, 502)
        self.assertIn("sem permissão", body["error"])

    def test_invalid_input_is_rejected_before_any_call(self):
        path = "/api/namespaces/shop/workloads/Deployment/api/apply"
        self.assertEqual(self.post(path, {"container": "app", "target": "tudo"})[0], 400)
        self.assertEqual(self.post(path, {"target": "workload"})[0], 400)
        self.assertEqual(self.post(path, {"container": "nope", "target": "workload"})[0], 404)
        self.assertEqual(self.post("/api/namespaces/shop/workloads/Deployment/ausente/apply",
                                   {"container": "app", "target": "workload"})[0], 404)
        self.assertEqual(self.kube_api.calls, [])


class PlanTests(unittest.TestCase):
    def test_plan_uses_only_items_with_suggestion(self):
        container = {"items": [
            {"kind": "request", "resource": "cpu", "current": 100.0, "suggested": 274},
            {"kind": "limit", "resource": "cpu", "current": 1000.0, "suggested": 365},
            {"kind": "request", "resource": "memory", "current": 512.0, "suggested": None},
        ]}
        planned = applymod.plan(container)
        self.assertEqual(planned["resources"], {"requests": {"cpu": "274m"}, "limits": {"cpu": "365m"}})
        self.assertEqual([c["to"] for c in planned["changes"]], ["274m", "365m"])

    def test_plan_without_suggestions_is_conflict(self):
        with self.assertRaises(applymod.ApplyError) as ctx:
            applymod.plan({"items": [{"kind": "request", "resource": "cpu", "current": 1.0, "suggested": None}]})
        self.assertEqual(ctx.exception.status, 409)

    def test_unsupported_kind_is_422_without_calling_the_api(self):
        class Recorder:
            def patch(self, path, body):
                raise AssertionError("não deveria chamar a API")
        workload = {"namespace": "shop", "kind": "Job", "name": "batch", "containers": [{
            "name": "app", "pods": [],
            "items": [{"kind": "request", "resource": "cpu", "current": 1.0, "suggested": 50}],
        }]}
        with self.assertRaises(applymod.ApplyError) as ctx:
            applymod.apply(Recorder(), workload, "app", "workload")
        self.assertEqual(ctx.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
