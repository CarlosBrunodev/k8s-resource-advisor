# Validação local (Docker + Prometheus falso)

Sobe a aplicação inteira na sua máquina, com métricas **sintéticas** no lugar do Prometheus.
Requisitos: Docker com `docker compose` v2, `make` e `curl`.

```bash
make up              # constrói as imagens, sobe tudo e aguarda ficar pronto
make check           # roda 31 verificações contra os cenários conhecidos
make up WINDOW=14    # recria com janela de 14 dias
ADMIN_TOKEN=segredo make up   # habilita a edição da fórmula na aba Fórmula (sem o token, ela fica desabilitada)
make logs            # acompanha os logs
make down            # para tudo
```

Endereços: interface http://localhost:8080 · API http://localhost:8081/api/overview · Prometheus falso http://localhost:9090
(troque as portas com `FRONT_PORT=`, `API_PORT=`, `PROM_PORT=`).

## Cenários do Prometheus falso (`fake-prometheus/server.py`)

| Namespace/workload | O que simula | Resultado esperado |
|---|---|---|
| shop/api | request CPU 100m, consumo ~200m, limits altos, 3 réplicas, restarts | request de CPU subdimensionado (sugerido ~275m), limit superdimensionado, workload em desvio |
| shop/web | request CPU 1000m, consumo ~150m | superdimensionado |
| shop/worker | StatefulSet sem request nem limit | sem configuração, workload pendente |
| shop/cache | request e limit próximos do ideal | sem alerta, sem "sem configuração" |
| shop/oom-app | limit de memória 256Mi e OOMKilled | limit de memória subdimensionado por OOMKilled |
| shop/recent-app | pod criado há 6 h | dados insuficientes, marcado como recente |
| payments/ledger | 2 réplicas, pod antigo recriado, request de CPU mudou há 2 dias | agrupa 2 réplicas, mantém o pod antigo no histórico, registra a mudança |
| payments/reports | lacuna de coleta de 5 dias | cobertura abaixo de 80%, dados insuficientes |
| payments/gateway | request de memória 512Mi para consumo ~300Mi | superdimensionado |

As séries são determinísticas: o mesmo cenário gera os mesmos valores em cada execução.
Os cenários valem para as janelas de 7 e de 14 dias (`make check` verifica a janela configurada).

## Importante

- O Prometheus falso existe **só para teste local**. Não use em cluster. Os manifestos em `k8s/` apontam para o Prometheus real.
- Os valores são sintéticos. Eles validam a lógica (classificação, agrupamento, filtros, CSV, falhas), não a aderência a um cluster real.
- `make test` roda os testes unitários do back-end dentro de um contêiner Python.
