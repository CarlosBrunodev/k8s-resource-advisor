# Validação das recomendações com PromQL

Consultas para reproduzir, direto no Prometheus, os cálculos feitos pelo Resource Advisor. Use-as para conferir uma
sugestão antes de aplicá-la. Metodologia completa em [METODOLOGIA.md](METODOLOGIA.md).

Substitua `<namespace>` e `<workload>` pelos valores do workload. Para um Deployment, os pods têm o nome
`<workload>-<hash-do-replicaset>-<id>`, por isso a regex `<workload>-.*`.

Acesso ao Prometheus no cluster:

```bash
kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090:9090
# abra http://localhost:9090
```

## 1. Pods do workload

```promql
kube_pod_info{namespace="<namespace>", pod=~"<workload>-.*"}
```

## 2. Requests e limits atuais

Cores para CPU e bytes para memória. Execute cada consulta separadamente.

```promql
max by (container, resource) (
  kube_pod_container_resource_requests{namespace="<namespace>", pod=~"<workload>-.*", resource=~"cpu|memory"}
)
```

```promql
max by (container, resource) (
  kube_pod_container_resource_limits{namespace="<namespace>", pod=~"<workload>-.*", resource=~"cpu|memory"}
)
```

## 3. CPU: referência P95 em milicores

Base do request sugerido. Multiplique pelo fator de margem (1,2 por padrão).

```promql
max by (container) (
  quantile_over_time(0.95,
    (sum by (pod, container) (rate(container_cpu_usage_seconds_total{namespace="<namespace>", pod=~"<workload>-.*", container!="", container!="POD"}[5m])))[7d:5m]
  )
) * 1000
```

## 4. CPU: pico P99 em milicores

Base do limit sugerido.

```promql
max by (container) (
  quantile_over_time(0.99,
    (sum by (pod, container) (rate(container_cpu_usage_seconds_total{namespace="<namespace>", pod=~"<workload>-.*", container!="", container!="POD"}[5m])))[7d:5m]
  )
) * 1000
```

## 5. Memória: referência P95 em Mi

Base do request sugerido.

```promql
max by (container) (
  quantile_over_time(0.95,
    (container_memory_working_set_bytes{namespace="<namespace>", pod=~"<workload>-.*", container!="", container!="POD"})[7d:5m]
  )
) / 1048576
```

## 6. Memória: pico máximo em Mi

Base do limit sugerido.

```promql
max by (container) (
  max_over_time(container_memory_working_set_bytes{namespace="<namespace>", pod=~"<workload>-.*", container!="", container!="POD"}[7d])
) / 1048576
```

## 7. Throttling de CPU em %

O app marca o limit de CPU como subdimensionado a partir de 5%.

```promql
max by (container) (
  quantile_over_time(0.95,
    (
      sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{namespace="<namespace>", pod=~"<workload>-.*", container!=""}[5m]))
      /
      sum by (pod, container) (rate(container_cpu_cfs_periods_total{namespace="<namespace>", pod=~"<workload>-.*", container!=""}[5m]))
    )[7d:5m]
  )
) * 100
```

## 8. OOMKilled na janela

Retorna 1 para cada container que teve OOMKilled nos últimos 7 dias. O app marca o limit de memória como
subdimensionado nesse caso.

```promql
max by (container) (
  max_over_time(kube_pod_container_status_last_terminated_reason{namespace="<namespace>", pod=~"<workload>-.*", reason="OOMKilled"}[7d])
)
```

## 9. Reinícios na janela

```promql
sum by (container) (
  increase(kube_pod_container_status_restarts_total{namespace="<namespace>", pod=~"<workload>-.*"}[7d])
)
```

## 10. Cobertura do histórico

Fração dos passos de 5 min com dados. O app exige pelo menos 0,80.

```promql
max by (container) (
  count_over_time(
    (container_memory_working_set_bytes{namespace="<namespace>", pod=~"<workload>-.*", container!="", container!="POD"} > 0)[7d:5m]
  )
) / (7 * 24 * 12)
```

## Como comparar com o app

| Consulta | Compare com |
|---|---|
| Consulta 3 × 1,2 | `request` sugerido de CPU |
| Consulta 4 × 1,2 | `limit` sugerido de CPU |
| Consulta 5 × 1,2 | `request` sugerido de memória |
| Consulta 6 × 1,2 | `limit` sugerido de memória |
| Consulta 7 > 5 | override de throttling: limit de CPU subdimensionado |
| Consulta 8 = 1 | override de OOM: limit de memória subdimensionado |
| Consulta 10 < 0,80 | dados insuficientes |

Lembre que o `limit` sugerido nunca fica abaixo do `request` sugerido. Se o cálculo der menos, o app usa o `request`.

Diferenças de alguns % são esperadas. O app calcula percentis sobre os pontos do passo automático da consulta
(15 min para 7 dias), e o PromQL usa a subquery `[7d:5m]`.

Se a consulta 1 não retornar nada, confira o namespace e o nome do Deployment com
`kube_deployment_created{deployment="<workload>"}`.
