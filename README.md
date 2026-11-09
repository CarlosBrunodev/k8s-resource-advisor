# Resource Advisor para Kubernetes

Aplicação de consulta que compara o consumo real de CPU e memória com os `requests` e `limits` configurados,
usando o Prometheus, e recomenda ajustes por namespace → workload → container → réplica.
Modo somente leitura: nenhum recurso do cluster é alterado. Os ajustes passam pelo fluxo de deploy ou GitOps da equipe.

## Estrutura

```
backend/   API em Python (biblioteca padrão, sem dependências externas)
  app/analysis.py    cálculo de percentis, sugestões, classificação, filtros e CSV
  app/prometheus.py  cliente HTTP do Prometheus e consultas PromQL
  app/service.py     cache com TTL, single-flight e resposta degradada
  app/formula.py     fórmula editável: validação, aplicação ao cálculo e gravação em arquivo
  app/server.py      rotas /api/* e probes
  tests/             29 testes (análise, filtros, CSV, API, fórmula e falhas), com Prometheus simulado
frontend/  interface estática (HTML/CSS/JS sem build) servida por nginx, com proxy de /api
  assets/            imagem da marca usada no canto superior esquerdo
k8s/       manifests (Deployments, Services, ConfigMap, exemplo de Secret) em kustomize
```

## Documentação

- [docs/METODOLOGIA.md](docs/METODOLOGIA.md): metodologia de cálculo, justificativas e limitações, com diagramas de arquitetura e de fluxo.
- [docs/arquitetura.svg](docs/arquitetura.svg): diagrama de arquitetura.
- [docs/validacao-promql.md](docs/validacao-promql.md): consultas PromQL para conferir uma sugestão no Prometheus.

## Métricas consultadas

| Finalidade | Métrica |
|---|---|
| Consumo de CPU | `container_cpu_usage_seconds_total` (kubelet/cAdvisor, via `rate` de 5 min) |
| Consumo de memória | `container_memory_working_set_bytes` (máximo por passo) |
| Throttling | `container_cpu_cfs_throttled_periods_total` / `container_cpu_cfs_periods_total` |
| `requests` / `limits` | `kube_pod_container_resource_requests` / `_limits` (kube-state-metrics) |
| Pod → controlador | `kube_pod_owner`, `kube_replicaset_owner` (série por intervalo, então pods recriados continuam no histórico) |
| OOMKilled | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` |
| Reinícios | `kube_pod_container_status_restarts_total` (incremento na janela) |
| Retenção | `/api/v1/status/runtimeinfo` (`storageRetention`) |

## Regras de cálculo

| Item | Regra padrão |
|---|---|
| Consumo de referência | P95 do consumo de cada réplica; o workload usa o **maior** valor entre as réplicas |
| `request` sugerido | referência × (1 + 20%), com piso de 10m e 32Mi |
| `limit` sugerido | CPU: P99 × (1 + 20%). Memória: máximo × (1 + 20%). Nunca abaixo do request sugerido |
| Classificação | compara o atual com o sugerido e aceita ±10% de tolerância |
| Override de CPU | throttling ≥ 5% no limit → **subdimensionado** |
| Override de memória | OOMKilled na janela no limit → **subdimensionado** |
| Dados insuficientes | cobertura < 80% ou histórico < 24 h → sem sugestão |
| Sem configuração | request ou limit ausente no container |
| Status do workload | **Com desvio** (vermelho) se houver ao menos um desvio; **Adequado** (verde) se todos os itens forem adequados; **Pendente** nos demais casos |

Exemplo do enunciado: request de 100m e consumo de referência de 200m com margem de 20% resultam em **240m sugeridos**,
**+140m** de ajuste, e a justificativa informa janela, estatística e margem. Coberto por `backend/tests/test_analysis.py`.

## Configuração (variáveis de ambiente do back-end)

| Variável | Padrão | Descrição |
|---|---|---|
| `PROMETHEUS_URL` | — (obrigatória) | Endereço do Service do Prometheus, ex.: `http://prometheus-operated.monitoring.svc:9090` |
| `PROMETHEUS_BEARER_TOKEN` / `PROMETHEUS_USERNAME` / `PROMETHEUS_PASSWORD` | vazio | Credenciais opcionais, carregadas de um Secret |
| `ANALYSIS_WINDOW_DAYS` | `7` | Janela móvel: `7` ou `14` (aceita de 1 a 30) |
| `PROMETHEUS_STEP_SECONDS` | automático | Passo das consultas; automático mantém ~700 pontos por série |
| `REFERENCE_PERCENTILE` | `95` | Percentil do consumo de referência |
| `CPU_LIMIT_PERCENTILE` / `MEMORY_LIMIT_PERCENTILE` | `99` / `100` | Pico usado para os limits |
| `REQUEST_MARGIN_PERCENT` / `LIMIT_MARGIN_PERCENT` | `20` / `20` | Margens |
| `TOLERANCE_PERCENT` | `10` | Faixa de "adequado" em torno do sugerido |
| `THROTTLING_THRESHOLD_PERCENT` | `5` | Throttling que marca o limit de CPU como subdimensionado |
| `MIN_COVERAGE_PERCENT` / `MIN_HISTORY_HOURS` | `80` / `24` | Requisitos mínimos de dados |
| `CACHE_TTL_SECONDS` | `300` | Reutilização do resultado da análise |
| `ADMIN_TOKEN` | vazio | Token da página de fórmula. Sem ele, a edição fica desabilitada |
| `FORMULA_FILE` | vazio | Arquivo JSON onde a fórmula editada é gravada. Sem ele, a edição vale só até o reinício |
| `PROMETHEUS_TIMEOUT_SECONDS` / `PROMETHEUS_MAX_CONCURRENCY` | `20` / `4` | Limites de tempo e de concorrência das consultas |
| `EXCLUDED_NAMESPACES` | `kube-system,kube-public,kube-node-lease` | Namespaces ignorados |
| `CPU_FLOOR_MILLICORES` / `MEMORY_FLOOR_MI` | `10` / `32` | Pisos das sugestões |

## Endpoints

| Método | Rota | Uso |
|---|---|---|
| GET | `/api/health/live` | liveness |
| GET | `/api/health/ready` | readiness: 200 se o Prometheus responde ou há resultado anterior; 503 caso contrário |
| GET | `/api/overview` | cartões por namespace, janela, qualidade dos dados |
| GET | `/api/namespaces` | lista de namespaces com resumo |
| GET | `/api/namespaces/{ns}?q=&status=&resource=&sort=` | workloads filtrados, totais de capacidade |
| GET | `/api/namespaces/{ns}/workloads/{kind}/{name}` | detalhe: containers, itens, réplicas, histórico de configuração, séries para os gráficos, YAML |
| GET | `/api/namespaces/{ns}/export.csv?q=&status=&resource=&sort=` | CSV com os filtros aplicados (separador `;` e BOM, para o Excel em pt-BR) |
| POST | `/api/analysis/refresh` | força um novo cálculo |
| GET | `/api/formula` | parâmetros da fórmula vigente e os textos de método |
| PUT | `/api/formula` | altera a fórmula (exige cabeçalho `X-Admin-Token`); valida, grava e recalcula |

Status de filtro aceitos: `desvio`, `adequado`, `pendente`, `subdimensionado`, `superdimensionado`,
`sem_configuracao`, `dados_insuficientes`. `sort`: `ajuste`, `pressao`, `nome`.

Falha do Prometheus: se houver um resultado anterior, a API responde com `meta.stale = true`, o motivo em `meta.error` e a
hora da última coleta bem-sucedida. A interface mostra um aviso. Nova tentativa a cada 30 s.

## Fórmula editável (página Fórmula)

A aba **Fórmula** permite alterar os parâmetros usados em todos os cálculos: janela de análise, percentil do consumo
de referência (request), percentil do pico de CPU e de memória (limit), margens do request e do limit, e a tolerância
de "adequado". Ao salvar, o back-end valida os valores, grava o arquivo `FORMULA_FILE`, descarta o resultado anterior
e recalcula. Os textos de "Método" e "Justificativa" passam a usar a fórmula nova.

- A alteração exige o token em `ADMIN_TOKEN`. A leitura é pública, como o restante da API.
- Não há edição de expressão livre: só os parâmetros acima. Um avaliador de expressões seria um próximo passo.
- No Kubernetes, crie o Secret `resource-advisor-admin` (veja `k8s/backend-secret.example.yaml`). O volume
  `resource-advisor-formula` (`k8s/formula-pvc.yaml`) guarda `formula.json` e sobrevive a reinícios.

## Validação local

Para testar sem cluster, use `make up` e `make check` (veja [LOCAL.md](LOCAL.md)). Ele sobe um Prometheus falso com cenários conhecidos.

## Instalação

```bash
# imagens
docker build -t resource-advisor-backend:0.1.0 backend/
docker build -t resource-advisor-frontend:0.1.0 frontend/
# envie as imagens ao registry da equipe e ajuste as tags em k8s/kustomization.yaml

# namespace, Secret (somente se o Prometheus exigir autenticação) e aplicação
kubectl create namespace resource-advisor
kubectl -n resource-advisor create secret generic resource-advisor-prometheus --from-literal=bearer-token="$TOKEN"
# edite PROMETHEUS_URL em k8s/backend.yaml antes de aplicar
kubectl apply -k k8s/

# acesso local
kubectl -n resource-advisor port-forward svc/resource-advisor-frontend 8080:80
# abra http://localhost:8080
```

Testes do back-end (sem cluster):

```bash
cd backend && python3 -m unittest discover -s tests -t .
```

## Limitações conhecidas

- **Não validado contra um cluster ou Prometheus reais.** Os testes usam um Prometheus simulado, que responde às mesmas
  consultas. A validação T01 e T07 (comparar com consultas diretas em dois namespaces) ainda precisa ser feita no ambiente.
- **Interface testada só em Chromium headless.** As telas e o fluxo da fórmula foram executados, mas não em outros navegadores.
- **Autorização por namespace não implementada.** O enunciado a condiciona a restrições de acesso entre equipes; hoje a API
  expõe todos os namespaces que não estão em `EXCLUDED_NAMESPACES`.
- **Percentis sobre o passo da consulta.** As séries de CPU têm a resolução do passo (15 ou 30 min). Picos curtos de CPU podem
  ser suavizados. A memória usa máximo por passo, então os picos são preservados.
- **Cobertura.** É a fração dos passos da janela em que ao menos um pod do container tem amostra. Recriações de pods não
  reduzem a cobertura; lacunas e workloads recentes reduzem.
- **OOMKilled é evidência, não contagem.** A métrica `last_terminated_reason` indica o motivo mais recente, por passo. Não conta cada evento.
- **Resolução de controladores.** CronJob aparece como Job. ReplicaSet órfão cai para o nome do Deployment, removendo o sufixo de hash.
- **Cache em memória e análise síncrona.** Use uma única réplica do back-end. Requisições simultâneas esperam a mesma análise.
- **Totais respeitam os filtros.** Itens sem configuração não entram nos totais de capacidade.
- **Retenção.** Se `storageRetention` não puder ser lido, a interface mostra "não identificada" e não marca insuficiência.
