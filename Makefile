UV ?= uv
EXTRAS ?= dev

.PHONY: install lint format-check typecheck test docs-check ci eval eval-live eval-record

install:
	$(UV) sync --extra $(EXTRAS)

lint:
	$(UV) run --extra $(EXTRAS) ruff check src tests scripts

format-check:
	$(UV) run --extra $(EXTRAS) ruff format --check src tests scripts

typecheck:
	$(UV) run --extra $(EXTRAS) mypy

test:
	$(UV) run --extra $(EXTRAS) pytest

# Estructura, nombres, cabeceras y enlaces de la documentacion (.agents/rules/).
docs-check:
	$(UV) run --extra $(EXTRAS) python scripts/check_docs.py

# Mismo orden y comandos que .github/workflows/ci.yml
ci: lint format-check typecheck test docs-check

# Evaluacion de la review (iniciativa 006). `EVAL_COMPARE` anade el delta contra
# el baseline congelado. `eval-live` y `eval-record` salen a la red (modelo real).
EVAL_COMPARE ?=

eval:
	$(UV) run --extra $(EXTRAS) python scripts/eval.py report $(if $(EVAL_COMPARE),--compare $(EVAL_COMPARE))

eval-live:
	$(UV) run --extra $(EXTRAS) python scripts/eval.py live

eval-record:
	$(UV) run --extra $(EXTRAS) python scripts/eval.py record
