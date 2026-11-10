import unittest

from app.analysis import MIB
from app.nodes import QUERIES, build_node_detail, build_nodes, collect_nodes
from tests.fakes import FakeProm

NOW = 1_700_000_000.0


def fleet():
    """Um control-plane e dois workers. worker-1 tem overcommit de limits de CPU; worker-2 está ocioso."""
    fake = FakeProm()
    fake.add_node("cp-1", cpu=2, mem=4096 * MIB, role="control-plane")
    fake.add_node("worker-1", cpu=4, mem=8192 * MIB)
    fake.add_node("worker-2", cpu=4, mem=8192 * MIB)
    fake.add_pod("shop", "api-1", deployment="api", node="worker-1", req_cpu=2.0, lim_cpu=3.0,
                 req_mem=2048 * MIB, lim_mem=4096 * MIB)
    fake.add_pod("shop", "web-1", deployment="web", node="worker-1", req_cpu=1.0, lim_cpu=3.0,
                 req_mem=2048 * MIB, lim_mem=4096 * MIB)
    fake.add_pod("shop", "api-2", deployment="api", node="worker-2", req_cpu=0.5, lim_cpu=1.0,
                 req_mem=512 * MIB, lim_mem=1024 * MIB)
    return fake


def view(fake, excluded=()):
    return build_nodes(collect_nodes(fake, NOW), excluded, NOW)


def node(result, name):
    return next(n for n in result["nodes"] if n["name"] == name)


class NodeSummaryTests(unittest.TestCase):
    def test_workers_exclude_control_plane(self):
        result = view(fleet())
        self.assertEqual(result["summary"]["workers"], 2)
        self.assertEqual(result["summary"]["controlPlane"], 1)
        self.assertEqual(node(result, "cp-1")["role"], "control-plane")

    def test_cpu_requests_and_limits_percentages(self):
        w1 = node(view(fleet()), "worker-1")
        self.assertEqual(w1["cpu"]["allocatable"], 4000.0)
        self.assertEqual(w1["cpu"]["requests"], 3000.0)
        self.assertEqual(w1["cpu"]["requestsPct"], 75.0)
        self.assertEqual(w1["cpu"]["limits"], 6000.0)
        self.assertEqual(w1["cpu"]["limitsPct"], 150.0)   # overcommit de limits
        self.assertEqual(w1["cpu"]["free"], 1000.0)

    def test_limits_overcommit_is_flagged_not_failed(self):
        w1 = node(view(fleet()), "worker-1")
        self.assertIn("overcommit de limits de CPU (150%)", w1["flags"])
        self.assertEqual(w1["status"], "atencao")

    def test_memory_totals_are_in_mi(self):
        result = view(fleet())
        w1 = node(result, "worker-1")
        self.assertEqual(w1["memory"]["allocatable"], 8192.0)
        self.assertEqual(w1["memory"]["requestsPct"], 50.0)
        self.assertEqual(w1["memory"]["limitsPct"], 100.0)   # no limite, não é overcommit
        self.assertEqual(result["summary"]["memory"]["allocatable"], 16384.0)

    def test_cluster_totals_sum_only_workers(self):
        totals = view(fleet())["summary"]["cpu"]
        self.assertEqual(totals["allocatable"], 8000.0)
        self.assertEqual(totals["requests"], 3500.0)
        self.assertEqual(totals["limits"], 7000.0)
        self.assertEqual(totals["free"], 4500.0)

    def test_imbalance_between_workers_is_reported(self):
        summary = view(fleet())["summary"]
        self.assertEqual(summary["spreadPct"], 62.5)     # 75% − 12,5%
        self.assertFalse(summary["balanced"])

    def test_balanced_workers(self):
        fake = FakeProm()
        fake.add_node("worker-1", cpu=4, mem=8192 * MIB)
        fake.add_node("worker-2", cpu=4, mem=8192 * MIB)
        fake.add_pod("shop", "a", node="worker-1", req_cpu=1.0, lim_cpu=1.0, req_mem=MIB, lim_mem=MIB)
        fake.add_pod("shop", "b", node="worker-2", req_cpu=1.0, lim_cpu=1.0, req_mem=MIB, lim_mem=MIB)
        summary = view(fake)["summary"]
        self.assertEqual(summary["spreadPct"], 0.0)
        self.assertTrue(summary["balanced"])

    def test_requests_near_capacity_are_flagged(self):
        fake = FakeProm()
        fake.add_node("worker-1", cpu=4, mem=8192 * MIB)
        fake.add_pod("shop", "a", node="worker-1", req_cpu=3.6, lim_cpu=3.6, req_mem=MIB, lim_mem=MIB)
        w1 = node(view(fake), "worker-1")
        self.assertTrue(any("altos" in f for f in w1["flags"]))

    def test_excluded_namespaces_do_not_count(self):
        fake = fleet()
        fake.add_pod("kube-system", "dns-1", node="worker-2", req_cpu=1.0, lim_cpu=1.0,
                     req_mem=MIB, lim_mem=MIB)
        w2 = node(view(fake, excluded=("kube-system",)), "worker-2")
        self.assertEqual(w2["cpu"]["requests"], 500.0)
        self.assertEqual(w2["pods"], 1)


class NodeStatusTests(unittest.TestCase):
    def test_node_not_ready_is_overloaded(self):
        fake = fleet()
        fake.nodes[2]["ready"] = False   # worker-2
        w2 = node(view(fake), "worker-2")
        self.assertEqual(w2["ready"], False)
        self.assertEqual(w2["status"], "sobrecarregado")
        self.assertIn("node não está Ready", w2["flags"])

    def test_unknown_readiness_when_metric_is_missing(self):
        raw = {key: [] for key in QUERIES}
        raw["info"] = [{"metric": {"node": "worker-1"}, "value": [NOW, "1"]}]
        w1 = build_nodes(raw, (), NOW)["nodes"][0]
        self.assertIsNone(w1["ready"])
        self.assertEqual(w1["status"], "ok")

    def test_containers_without_limit_are_counted(self):
        fake = fleet()
        fake.add_pod("shop", "batch-1", node="worker-2", req_cpu=0.2, lim_cpu=None,
                     req_mem=128 * MIB, lim_mem=256 * MIB)
        w2 = node(view(fake), "worker-2")
        self.assertEqual(w2["containersWithoutLimit"], 1)
        self.assertTrue(any("sem limit" in f for f in w2["flags"]))


class NodeDetailTests(unittest.TestCase):
    def test_containers_sorted_by_cpu_request(self):
        raw = collect_nodes(fleet(), NOW)
        detail = build_node_detail(raw, (), "worker-1")
        self.assertEqual(detail["pods"], 2)
        names = [c["pod"] for c in detail["containers"]]
        self.assertEqual(names, ["api-1", "web-1"])
        self.assertEqual(detail["containers"][0]["cpuRequest"], 2000.0)
        self.assertEqual(detail["containers"][0]["cpuLimit"], 3000.0)
        self.assertEqual(detail["containers"][0]["memoryRequest"], 2048.0)

    def test_missing_limit_is_none(self):
        fake = fleet()
        fake.add_pod("shop", "batch-1", node="worker-2", req_cpu=0.2, lim_cpu=None,
                     req_mem=128 * MIB, lim_mem=None)
        detail = build_node_detail(collect_nodes(fake, NOW), (), "worker-2")
        batch = next(c for c in detail["containers"] if c["pod"] == "batch-1")
        self.assertIsNone(batch["cpuLimit"])
        self.assertIsNone(batch["memoryLimit"])

    def test_unknown_node_returns_none(self):
        self.assertIsNone(build_node_detail(collect_nodes(fleet(), NOW), (), "nao-existe"))


if __name__ == "__main__":
    unittest.main()
