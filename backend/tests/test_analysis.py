import csv
import io
import unittest

from app.analysis import (
    MIB, analyze, export_csv, filter_workloads, percentile, resolve_workload,
)
from app.prometheus import Collected, collect, parse_retention
from tests.fakes import FakeProm, make_settings

NOW = 1_700_000_000.0
DAY = 86400


def run(fake, **settings):
    s = make_settings(**settings)
    col = collect(fake, s, NOW)
    return analyze(col, s, NOW), col


def item(workload, container, resource, kind):
    return next(i for i in workload["items"]
                if i["container"] == container and i["resource"] == resource and i["kind"] == kind)


def base_fake():
    """Caso do enunciado: request de CPU 100m, consumo de 200m por 7 dias."""
    fake = FakeProm()
    fake.add_rs("shop", "api-7d9f8b6c5d", "api")
    fake.add_pod("shop", "api-7d9f8b6c5d-x1", deployment="api", rs="api-7d9f8b6c5d",
                 cpu=0.2, mem=300 * MIB, req_cpu=0.1, req_mem=256 * MIB,
                 lim_cpu=0.5, lim_mem=512 * MIB)
    return fake


class PercentileAndParsingTests(unittest.TestCase):
    def test_percentile_nearest_interpolation(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(percentile([1, 2, 3, 4, 5], 100), 5)
        self.assertIsNone(percentile([], 95))

    def test_retention_parsing(self):
        self.assertEqual(parse_retention("15d"), 15 * DAY)
        self.assertEqual(parse_retention("720h"), 720 * 3600)
        self.assertEqual(parse_retention("30m"), 30 * 60)
        self.assertIsNone(parse_retention(""))


class RecommendationTests(unittest.TestCase):
    def test_spec_example_request_and_limit(self):
        snap, _ = run(base_fake())
        wl = snap["workloads"][0]
        cpu_req = item(wl, "app", "cpu", "request")
        self.assertEqual(cpu_req["current"], 100)
        self.assertEqual(cpu_req["reference"], 200)
        self.assertEqual(cpu_req["suggested"], 240)
        self.assertEqual(cpu_req["adjustment"], 140)
        self.assertEqual(cpu_req["status"], "subdimensionado")
        self.assertIn("P95", cpu_req["method"])
        self.assertIn("20%", cpu_req["method"])
        self.assertIn("240m", cpu_req["justification"])

    def test_limits_are_evaluated_separately(self):
        snap, _ = run(base_fake())
        wl = snap["workloads"][0]
        cpu_lim = item(wl, "app", "cpu", "limit")
        mem_lim = item(wl, "app", "memory", "limit")
        self.assertEqual(cpu_lim["suggested"], 250)      # 200m × 1,2 = 240m, elevado ao piso de 250m
        self.assertEqual(cpu_lim["status"], "superdimensionado")  # 500m configurado
        self.assertEqual(mem_lim["suggested"], 360)      # pico 300Mi × 1,2
        self.assertEqual(mem_lim["status"], "superdimensionado")
        self.assertEqual(wl["status"], "desvio")

    def test_adequate_within_tolerance(self):
        fake = base_fake()
        fake.pods[0]["req_cpu"] = 0.24
        fake.pods[0]["req_mem"] = 360 * MIB
        snap, _ = run(fake)
        wl = snap["workloads"][0]
        self.assertEqual(item(wl, "app", "cpu", "request")["status"], "adequado")
        self.assertEqual(item(wl, "app", "memory", "request")["status"], "adequado")

    def test_missing_request_is_without_configuration_but_keeps_suggestion(self):
        fake = base_fake()
        fake.pods[0]["req_cpu"] = None
        snap, _ = run(fake)
        cpu_req = item(snap["workloads"][0], "app", "cpu", "request")
        self.assertEqual(cpu_req["status"], "sem_configuracao")
        self.assertEqual(cpu_req["suggested"], 240)
        self.assertIsNone(cpu_req["adjustment"])

    def test_short_history_is_insufficient(self):
        fake = base_fake()
        fake.pods[0]["first"] = NOW - 2 * 3600
        fake.pods[0]["last"] = NOW
        snap, _ = run(fake)
        cpu_req = item(snap["workloads"][0], "app", "cpu", "request")
        self.assertEqual(cpu_req["status"], "dados_insuficientes")
        self.assertIsNone(cpu_req["suggested"])
        self.assertIn("histórico", cpu_req["justification"])

    def test_cpu_throttling_marks_limit_as_under_sized(self):
        fake = base_fake()
        fake.pods[0]["lim_cpu"] = 0.5
        fake.pods[0]["throttle"] = 0.2
        snap, _ = run(fake)
        cpu_lim = item(snap["workloads"][0], "app", "cpu", "limit")
        self.assertEqual(cpu_lim["status"], "subdimensionado")
        self.assertEqual(cpu_lim["statusReason"], "throttling")
        self.assertIn("Status definido por throttling", cpu_lim["justification"])
        self.assertIn("P99", cpu_lim["method"])

    def test_throttling_steps_up_from_current_limit(self):
        fake = base_fake()
        fake.pods[0]["lim_cpu"] = 0.5          # limit atual 500m, consumo constante 200m
        fake.pods[0]["throttle"] = 0.2
        snap, _ = run(fake)
        cpu_lim = item(snap["workloads"][0], "app", "cpu", "limit")
        self.assertEqual(cpu_lim["suggested"], 600)   # 500m × 1,2, acima do P99 (240m)
        self.assertEqual(cpu_lim["adjustment"], 100)

    def test_throttling_uses_p99_when_it_is_higher_than_the_step(self):
        fake = base_fake()
        fake.pods[0]["lim_cpu"] = 0.1          # 100m × 1,2 = 120m < P99 200m × 1,2 = 240m, e abaixo do piso
        fake.pods[0]["throttle"] = 0.2
        snap, _ = run(fake)
        cpu_lim = item(snap["workloads"][0], "app", "cpu", "limit")
        self.assertEqual(cpu_lim["suggested"], 250)   # piso de 250m
        self.assertEqual(cpu_lim["reference"], 200)

    def test_cpu_floors_apply_to_request_and_limit(self):
        fake = base_fake()
        fake.pods[0]["cpu"] = 0.02             # consumo de 20m: abaixo dos pisos
        fake.pods[0]["req_cpu"] = 0.1
        fake.pods[0]["lim_cpu"] = 0.5
        snap, _ = run(fake)
        wl = snap["workloads"][0]
        cpu_req = item(wl, "app", "cpu", "request")
        cpu_lim = item(wl, "app", "cpu", "limit")
        self.assertEqual(cpu_req["suggested"], 100)   # piso de request
        self.assertEqual(cpu_lim["suggested"], 250)   # piso de limit
        self.assertIn("Mínimo de boa prática de CPU aplicado: 100m", cpu_req["justification"])
        self.assertIn("Mínimo de boa prática de CPU aplicado: 250m", cpu_lim["justification"])

    def test_floor_is_not_reported_when_calculation_is_above_it(self):
        snap, _ = run(base_fake())
        cpu_req = item(snap["workloads"][0], "app", "cpu", "request")
        self.assertNotIn("Mínimo de boa prática", cpu_req["justification"])

    def test_without_throttling_no_status_reason(self):
        snap, _ = run(base_fake())
        cpu_lim = item(snap["workloads"][0], "app", "cpu", "limit")
        self.assertIsNone(cpu_lim["statusReason"])
        self.assertIn("P99", cpu_lim["method"])

    def test_oom_marks_memory_limit_as_under_sized(self):
        fake = base_fake()
        fake.pods[0]["oom"] = 3
        snap, _ = run(fake)
        mem_lim = item(snap["workloads"][0], "app", "memory", "limit")
        self.assertEqual(mem_lim["status"], "subdimensionado")
        self.assertEqual(mem_lim["statusReason"], "oom")
        self.assertIn("OOMKilled", mem_lim["justification"])


class GroupingTests(unittest.TestCase):
    def test_recreated_pods_stay_in_the_same_workload(self):
        fake = FakeProm()
        fake.add_rs("shop", "api-aaaa11111", "api")
        fake.add_rs("shop", "api-bbbb22222", "api")
        # pod antigo, já removido, e pod atual de outro ReplicaSet do mesmo Deployment
        fake.add_pod("shop", "api-aaaa11111-old", rs="api-aaaa11111", cpu=0.1, mem=100 * MIB,
                     req_cpu=0.1, req_mem=128 * MIB, first=NOW - 3 * DAY, last=NOW - 2 * DAY)
        fake.add_pod("shop", "api-bbbb22222-new", rs="api-bbbb22222", cpu=0.1, mem=100 * MIB,
                     req_cpu=0.1, req_mem=128 * MIB)
        snap, _ = run(fake)
        self.assertEqual(len(snap["workloads"]), 1)
        wl = snap["workloads"][0]
        self.assertEqual((wl["kind"], wl["name"]), ("Deployment", "api"))
        self.assertEqual(wl["replicas"], 1)  # só o pod atual conta como réplica ativa
        self.assertEqual(len(wl["containers"][0]["pods"]), 2)

    def test_replicaset_without_owner_falls_back_to_deployment_name(self):
        col = Collected(window_start=0, window_end=1, step=1, retention_seconds=None)
        col.pod_owner[("shop", "api-7d9f8b6c5d-x1")] = ("ReplicaSet", "api-7d9f8b6c5d")
        self.assertEqual(resolve_workload(col, "shop", "api-7d9f8b6c5d-x1"), ("Deployment", "api"))
        self.assertEqual(resolve_workload(col, "shop", "solo"), ("Pod", "solo"))


class ExportAndFilterTests(unittest.TestCase):
    def test_filters_and_csv_scope(self):
        fake = base_fake()
        fake.add_rs("shop", "web-ccccc3333", "web")
        # web: consumo 100m/100Mi com request e limit de 120m/120Mi → tudo adequado
        fake.add_pod("shop", "web-ccccc3333-z", deployment="web", rs="web-ccccc3333",
                     cpu=0.1, mem=100 * MIB, req_cpu=0.12, req_mem=120 * MIB,
                     lim_cpu=0.12, lim_mem=120 * MIB)
        snap, _ = run(fake)
        only_desvio = filter_workloads(snap, "shop", status="desvio")
        self.assertEqual([w["name"] for w in only_desvio], ["api"])
        mem_only = filter_workloads(snap, "shop", resource="memory")
        self.assertTrue(all(i["resource"] == "memory" for w in mem_only for i in w["items"]))
        self.assertEqual(filter_workloads(snap, "other"), [])

        rows = list(csv.reader(io.StringIO(export_csv(
            only_desvio, snap, "shop", {"status": "desvio"}))))
        self.assertEqual(rows[0][0], "Workload")
        self.assertEqual(len(rows), 1 + 4)  # um container, quatro itens
        row = {h: v for h, v in zip(rows[0], rows[1])}
        self.assertEqual(row["Atual"], "100.0")
        self.assertEqual(row["Sugerido"], "240.0")
        self.assertEqual(row["Ajuste"], "+140.0")
        self.assertEqual(row["Workload"], "api")  # repetido em cada linha
        self.assertIn("status=desvio", row["Filtros"])
        # sem filtros reais, o padrão de ordenação não aparece como filtro
        plain = list(csv.reader(io.StringIO(export_csv(only_desvio, snap, "shop", {"sort": "ajuste"}))))
        self.assertEqual(plain[1][13], "sem filtros")


if __name__ == "__main__":
    unittest.main()
