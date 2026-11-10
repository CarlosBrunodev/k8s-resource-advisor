# Ambiente local do Resource Advisor (Prometheus falso + back-end + front-end, tudo em Docker).
# Uso: make help

PROJECT      := resource-advisor-local
# Detecta o Compose disponível: plugin 'docker compose' (v2) ou o binário 'docker-compose' (v1).
COMPOSE_BIN  := $(shell if docker compose version >/dev/null 2>&1; then echo 'docker compose'; elif command -v docker-compose >/dev/null 2>&1; then echo 'docker-compose'; fi)
COMPOSE      := $(COMPOSE_BIN) -p $(PROJECT)
WINDOW       ?= 7
FRONT_PORT   ?= 8080
API_PORT     ?= 8081
PROM_PORT    ?= 9090
export WINDOW FRONT_PORT API_PORT PROM_PORT

# Publicação: o repositório de cada imagem vem da linha 'image:' dos manifests de deploy (sem a tag).
API_IMAGE := $(shell grep -m1 'image:' k8s/backend.yaml | sed 's/.*image: *//; s/:[^:/]*$$//')
FRT_IMAGE := $(shell grep -m1 'image:' k8s/frontend.yaml | sed 's/.*image: *//; s/:[^:/]*$$//')

# Permite 'make tag 2.1.1': o número que vem depois de 'tag' vira a versão (e não um alvo).
ifeq ($(firstword $(MAKECMDGOALS)),tag)
TAG_VERSION := $(word 2,$(MAKECMDGOALS))
ifneq ($(TAG_VERSION),)
$(eval $(TAG_VERSION):;@:)
endif
endif
VERSION ?= $(TAG_VERSION)

# Falha cedo com diagnóstico quando nenhum Compose está acessível.
REQUIRE_COMPOSE = @test -n '$(COMPOSE_BIN)' || { echo 'ERRO: Docker Compose não encontrado. Rode: make doctor'; exit 1; }

.DEFAULT_GOAL := help
.PHONY: help build up wait urls down restart logs ps test check reset clean doctor tag

help: ## Lista os comandos
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Variáveis: WINDOW=7|14 (janela de análise), FRONT_PORT, API_PORT, PROM_PORT"
	@echo "Exemplo:   make up WINDOW=14"

build: ## Constrói as três imagens (Prometheus falso, back-end, front-end)
	$(REQUIRE_COMPOSE)
	$(COMPOSE) build

up: ## Constrói e sobe tudo em segundo plano, aguarda ficar pronto e mostra os endereços
	$(REQUIRE_COMPOSE)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory wait
	@$(MAKE) --no-print-directory urls

wait:
	@echo "Aguardando o back-end (a primeira análise pode levar alguns segundos)..."
	@for i in $$(seq 1 60); do \
	  if curl -fsS http://localhost:$(API_PORT)/api/health/ready >/dev/null 2>&1; then \
	    curl -fsS http://localhost:$(API_PORT)/api/overview >/dev/null 2>&1; echo "pronto."; exit 0; fi; \
	  sleep 2; done; echo "O back-end não ficou pronto. Veja: make logs"; exit 1

urls: ## Mostra os endereços locais
	@echo ""
	@echo "  Interface:          http://localhost:$(FRONT_PORT)"
	@echo "  API (direto):       http://localhost:$(API_PORT)/api/overview"
	@echo "  Prometheus falso:   http://localhost:$(PROM_PORT)/api/v1/status/buildinfo"
	@echo "  Janela de análise:  $(WINDOW) dias"

down: ## Para e remove os contêineres (mantém as imagens)
	$(REQUIRE_COMPOSE)
	$(COMPOSE) down

restart: ## Reinicia tudo (útil após trocar WINDOW ou editar o código)
	$(REQUIRE_COMPOSE)
	$(COMPOSE) down
	@$(MAKE) --no-print-directory up

logs: ## Acompanha os logs (Ctrl+C para sair)
	$(REQUIRE_COMPOSE)
	$(COMPOSE) logs -f --tail=100

ps: ## Estado dos contêineres
	$(REQUIRE_COMPOSE)
	$(COMPOSE) ps

test: ## Roda os testes unitários do back-end dentro de um contêiner Python
	docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR)/backend:/srv" -w /srv python:3.12-slim \
	  python -W ignore -m unittest discover -s tests -t .

check: ## Valida a API contra os cenários do Prometheus falso (exige make up)
	docker run --rm --network $(PROJECT)_default -v "$(CURDIR)/local:/w:ro" -w /w python:3.12-slim \
	  python check_local.py --api http://backend:8080 --window $(WINDOW)

reset: ## Derruba tudo e apaga os volumes
	$(REQUIRE_COMPOSE)
	$(COMPOSE) down -v

clean: ## Derruba tudo e remove as imagens locais construídas por este projeto
	$(REQUIRE_COMPOSE)
	$(COMPOSE) down -v --rmi local


tag: ## Constrói e publica as duas imagens com a versão informada. Uso: make tag 2.1.1
	@test -n '$(VERSION)' || { echo 'ERRO: informe a versão. Exemplo: make tag 2.1.1'; exit 1; }
	@test -n '$(API_IMAGE)' -a -n '$(FRT_IMAGE)' || { echo 'ERRO: não consegui ler as imagens de k8s/*.yaml'; exit 1; }
	@echo "Back-end:  $(API_IMAGE):$(VERSION)"
	@echo "Front-end: $(FRT_IMAGE):$(VERSION)"
	docker build -t $(API_IMAGE):$(VERSION) backend/
	docker build -t $(FRT_IMAGE):$(VERSION) frontend/
	docker push $(API_IMAGE):$(VERSION)
	docker push $(FRT_IMAGE):$(VERSION)

doctor: ## Mostra qual Docker/Compose será usado e se o Docker está acessível
	@docker --version
	@echo "Compose: $(COMPOSE)"
	@test -n "$(COMPOSE_BIN)" && $(COMPOSE) version || echo 'Compose não encontrado: instale o plugin docker compose v2 ou docker-compose'
