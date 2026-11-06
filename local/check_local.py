"""Verifica a API contra os cenários do Prometheus falso (fake-prometheus/server.py).

Uso: python check_local.py --api http://localhost:8081 [--window 7]
Sai com código 1 se algum cenário não bater.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

results = []


def get(api, path):
    try:
        with urllib.request.urlopen(api + path, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_json(api, path):
    status, body = get(api, path)
    return status, json.loads(body or b"{}")


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def item(workload, resource, kind):
    return next(i for i in workload["items"] if i["resource"] == resource and i["kind"] == kind)


def workload(api, ns, kind, name):
    status, body = get_json(api, f"/api/namespaces/{ns}/workloads/{kind}/{name}")
    return body["workload"] if status == 200 else None, body.get("charts", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8081")
    parser.add_argument("--window", type=int, default=7)
    args = parser.parse_args()
    api = args.api.rstrip("/")

    status, _ = get(api, "/api/health/ready")
    check("readiness responde 200", status == 200, f"HTTP {status}")
    if status != 200:
        report()
        return

    status, ov = get_json(api, "/api/overview")
    check("overview responde 200", status == 200, f"HTTP {status}")
    names = {n["name"] for n in ov.get("namespaces", [])} if status == 200 else set()
    check("namespaces shop e payments aparecem", {"shop", "payments"} <= names, str(sorted(names)))
    check("janela configurada", ov.get("window", {}).get("days") == args.window,
          f"esperado {args.window}, veio {ov.get('window', {}).get('days')}")
    check("retenção identificada e suficiente", ov.get("quality", {}).get("retentionSufficient") is True)
    check("meta sem desatualização", ov.get("meta", {}).get("stale") is False)

    api_wl, api_charts = workload(api, "shop", "Deployment", "api")
    check("shop/api é subdimensionado no request de CPU",
          api_wl and item(api_wl, "cpu", "request")["status"] == "subdimensionado")
    if api_wl:
        req = item(api_wl, "cpu", "request")
        check("shop/api: atual 100m e sugerido perto de 240m (consumo ~200m, margem 20%)",
              req["current"] == 100 and 200 <= req["suggested"] <= 300,
              f"atual={req['current']} sugerido={req['suggested']} ajuste={req['adjustment']}")
        check("shop/api: CPU limit 500m é superdimensionado",
              item(api_wl, "cpu", "limit")["status"] == "superdimensionado")
        check("shop/api: 3 réplicas ativas", api_wl["replicas"] == 3, f"veio {api_wl['replicas']}")
        check("shop/api: workload em desvio (vermelho)", api_wl["status"] == "desvio")
        check("shop/api: gráficos com consumo", any(c["cpu"]["consumption"] for c in api_charts))
        check("shop/api: YAML com requests sugeridos", "requests:" in api_wl["containers"][0]["yaml"])

    web, _ = workload(api, "shop", "Deployment", "web")
    check("shop/web tem request de CPU superdimensionado",
          web and item(web, "cpu", "request")["status"] == "superdimensionado")

    worker, _ = workload(api, "shop", "StatefulSet", "worker")
    check("shop/worker sem configuração no request de CPU",
          worker and item(worker, "cpu", "request")["status"] == "sem_configuracao")
    check("shop/worker com sugestão mesmo sem configuração",
          worker and item(worker, "cpu", "request")["suggested"] is not None)
    check("shop/worker fica pendente (cinza)", worker and worker["status"] == "pendente")

    cache, _ = workload(api, "shop", "Deployment", "cache")
    check("shop/cache não cai em sem configuração",
          cache and item(cache, "cpu", "request")["status"] != "sem_configuracao")

    oom, _ = workload(api, "shop", "Deployment", "oom-app")
    check("shop/oom-app: limit de memória subdimensionado por OOMKilled",
          oom and item(oom, "memory", "limit")["status"] == "subdimensionado"
          and "OOMKilled" in item(oom, "memory", "limit")["justification"])

    recent, _ = workload(api, "shop", "Deployment", "recent-app")
    check("shop/recent-app com dados insuficientes (pod de 6 h)",
          recent and item(recent, "cpu", "request")["status"] == "dados_insuficientes")
    check("shop/recent-app marcado como recente", recent and recent["recent"] is True)

    ledger, _ = workload(api, "payments", "Deployment", "ledger")
    check("payments/ledger agrupa 2 réplicas ativas", ledger and ledger["replicas"] == 2,
          f"veio {ledger and ledger['replicas']}")
    check("payments/ledger mantém o pod recriado no histórico",
          ledger and len(ledger["containers"][0]["pods"]) == 3,
          f"pods no histórico: {ledger and len(ledger['containers'][0]['pods'])}")
    if ledger:
        changes = ledger["containers"][0]["config"]["cpu_request"]
        check("payments/ledger: histórico registra mudança de request de CPU",
              len(changes) >= 2, f"mudanças: {changes}")

    reports, _ = workload(api, "payments", "Deployment", "reports")
    check("payments/reports com lacuna -> dados insuficientes",
          reports and item(reports, "cpu", "request")["status"] == "dados_insuficientes")
    check("payments/reports reporta cobertura abaixo de 80%",
          reports and reports["coverage"] < 0.8, f"cobertura={reports and reports['coverage']}")

    gateway, _ = workload(api, "payments", "Deployment", "gateway")
    check("payments/gateway: request de memória superdimensionado",
          gateway and item(gateway, "memory", "request")["status"] == "superdimensionado")

    status, flt = get_json(api, "/api/namespaces/shop?status=desvio")
    desvio_names = sorted(w["name"] for w in flt.get("workloads", [])) if status == 200 else []
    check("filtro status=desvio retorna só workloads em desvio",
          status == 200 and "api" in desvio_names and "worker" not in desvio_names, str(desvio_names))

    status, flt = get_json(api, "/api/namespaces/payments?resource=memory&sort=pressao")
    check("filtro resource=memory só traz itens de memória",
          status == 200 and all(i["resource"] == "memory" for w in flt["workloads"] for i in w["items"]))

    status, body = get(api, "/api/namespaces/shop/export.csv?status=desvio")
    text = body.decode("utf-8-sig") if status == 200 else ""
    check("CSV do namespace shop com filtro", status == 200 and text.startswith("Workload,Tipo de workload,")
          and "status=desvio" in text, f"HTTP {status}")

    status, _ = get(api, "/api/namespaces/inexistente")
    check("namespace inexistente retorna 404", status == 404, f"HTTP {status}")

    report()


def report():
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        extra = f"  ({detail})" if detail and not ok else ""
        print(f"[{mark}] {name}{extra}")
    print(f"\n{len(results) - len(failed)}/{len(results)} verificações passaram")
    sys.exit(1 if failed or not results else 0)


if __name__ == "__main__":
    main()
