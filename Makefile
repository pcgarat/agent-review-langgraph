UV ?= uv
EXTRAS ?= dev
# Plugin (`docker compose`) o binario (`docker-compose`); igual que scripts/smoke-nivel2.sh.
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help install lint format format-check typecheck fix test test-live docs-check ci \
	eval eval-live eval-record bootstrap twg-credentials ensure-dev-secrets \
	frontend-install frontend-check frontend-build panel-dev \
	up panel claude panel-claude down logs ps status smoke clean

##@ Dependencias

install: ## Sincroniza el entorno con los extras de desarrollo
	$(UV) sync --extra $(EXTRAS)

##@ Calidad

lint: ## Ruff (solo comprueba)
	$(UV) run --extra $(EXTRAS) ruff check src tests scripts

format: ## Aplica el formato (ruff format)
	$(UV) run --extra $(EXTRAS) ruff format src tests scripts

format-check: ## Comprueba el formato sin escribir
	$(UV) run --extra $(EXTRAS) ruff format --check src tests scripts

typecheck: ## Mypy (strict)
	$(UV) run --extra $(EXTRAS) mypy

fix: ## Arregla lint y formato automaticamente
	$(UV) run --extra $(EXTRAS) ruff check --fix src tests scripts
	$(UV) run --extra $(EXTRAS) ruff format src tests scripts

# `T=` acota a un fichero o expresion: `make test T=tests/test_web.py`.
test: ## Tests (T=<ruta o -k expr> para acotar)
	$(UV) run --extra $(EXTRAS) pytest $(T)

# El marcador `live` sale a la red y necesita credenciales reales; pytest lo
# excluye por defecto (addopts), asi que aqui se pide en positivo.
test-live: ## Tests marcados `live` (red y credenciales reales)
	$(UV) run --extra $(EXTRAS) pytest -m live

# Estructura, nombres, cabeceras y enlaces de la documentacion (.agents/rules/).
docs-check: ## Comprueba estructura, cabeceras y enlaces de la documentacion
	$(UV) run --extra $(EXTRAS) python scripts/check_docs.py

# Mismo orden y comandos que .github/workflows/ci.yml
ci: lint format-check typecheck test docs-check ## Lint + formato + tipos + tests + docs (igual que el CI)

##@ Frontend del panel (iniciativa 029)

# El panel en React vive en `frontend/`. `node_modules` y `dist` no se versionan.
FRONTEND ?= frontend

frontend-install: ## Instala las dependencias del panel (npm ci)
	cd $(FRONTEND) && npm ci

frontend-check: ## Tipos, lint y tests del panel
	cd $(FRONTEND) && npm run typecheck && npm run lint && npm test

frontend-build: ## Build de produccion del panel
	cd $(FRONTEND) && npm run build

panel-dev: ## Vite en :5173 con proxy a un `web` ya levantado en :8090
	cd $(FRONTEND) && npm run dev

##@ Evaluacion de la review (iniciativa 006)

# `EVAL_COMPARE` anade el delta contra el baseline congelado. `eval-live` y
# `eval-record` salen a la red (modelo real).
EVAL_COMPARE ?=

eval: ## Informe del gold set (replay, sin red)
	$(UV) run --extra $(EXTRAS) python scripts/eval.py report $(if $(EVAL_COMPARE),--compare $(EVAL_COMPARE))

# `REVIEW_TOOLS_ROLES=correctness` mide la variante con herramientas; lee la
# grabacion aparte (`--recording recorded_tools.json`) para no pisar el baseline
# de 008 (014 RH11).
eval-tools: ## Informe del gold set con el rol de herramientas activado (replay)
	REVIEW_TOOLS_ROLES=correctness \
	  $(UV) run --extra $(EXTRAS) python scripts/eval.py report --recording recorded_tools.json $(if $(EVAL_COMPARE),--compare $(EVAL_COMPARE))

eval-verifier: ## Informe del gold set con verificador L2 (escenario de produccion, replay)
	REVIEW_TOOLS_ROLES=correctness \
	  $(UV) run --extra $(EXTRAS) python scripts/eval.py report --recording recorded_tools.json --verifier on

eval-record-verifier: ## Regraba el gold set con el revisor y el verificador (red)
	LLM_REVIEWER_ENABLED=true REVIEW_TOOLS_ROLES=correctness \
	  $(UV) run --extra $(EXTRAS) python scripts/eval.py record --recording recorded_tools.json --verifier on

eval-live: ## Mide contra el modelo real (red)
	$(UV) run --extra $(EXTRAS) python scripts/eval.py live

eval-record: ## Regraba las respuestas del gold set (red)
	$(UV) run --extra $(EXTRAS) python scripts/eval.py record

eval-record-tools: ## Regraba el gold set con el rol de herramientas activado (red)
	REVIEW_TOOLS_ROLES=correctness \
	  $(UV) run --extra $(EXTRAS) python scripts/eval.py record --recording recorded_tools.json

##@ Entorno local (docker compose)

bootstrap: ## Crea .env desde .env.example y resume lo que falta
	scripts/bootstrap-env.sh

twg-credentials: ## Copia tu sesion twg al .env (solo desarrollo)
	scripts/sync-twg-credentials.sh

ensure-dev-secrets: ## Rellena URL y secretos locales si siguen en blanco
	$(UV) run --extra $(EXTRAS) python scripts/ensure_dev_secrets.py

up: ensure-dev-secrets ## Levanta el stack (build incluido)
	$(COMPOSE) up --build -d

panel: ensure-dev-secrets ## Levanta el stack con el panel de operacion (perfil `panel`)
	$(COMPOSE) --profile panel up --build -d

claude: ensure-dev-secrets ## Levanta el stack con el runner de Claude (perfil `claude`)
	$(COMPOSE) --profile claude up --build -d

panel-claude: ensure-dev-secrets ## Panel y runner de Claude (perfiles `panel` y `claude`)
	$(COMPOSE) --profile panel --profile claude up --build -d

down: ## Para el stack
	$(COMPOSE) down

# `S=` elige el servicio: `make logs S=api`.
logs: ## Sigue los logs (S=<servicio>, por defecto worker)
	$(COMPOSE) logs -f $(if $(S),$(S),worker)

ps: ## Estado de los servicios
	$(COMPOSE) ps

status: ## Contenedores, URLs y prueba de las credenciales de integracion
	COMPOSE="$(COMPOSE)" $(UV) run --extra $(EXTRAS) python scripts/status.py

# Smoke Nivel 2 real: deposita el artefacto del CI y espera la review en el PR.
smoke: ## Smoke Nivel 2 contra un PR real (PR=<id> [PIPELINE=<id>])
	scripts/smoke-nivel2.sh $(PR) $(PIPELINE)

##@ Utilidades

help: ## Muestra esta ayuda
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)}' $(MAKEFILE_LIST)

clean: ## Borra caches (pytest, ruff, mypy, __pycache__)
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find src tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
