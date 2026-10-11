UV ?= uv
EXTRAS ?= dev

.PHONY: install lint format-check typecheck test docs-check ci

install:
	$(UV) sync --extra $(EXTRAS)

lint:
	$(UV) run --extra $(EXTRAS) ruff check src tests

format-check:
	$(UV) run --extra $(EXTRAS) ruff format --check src tests

typecheck:
	$(UV) run --extra $(EXTRAS) mypy

test:
	$(UV) run --extra $(EXTRAS) pytest

# Estructura, nombres, cabeceras y enlaces de la documentacion (.agents/rules/).
docs-check:
	$(UV) run --extra $(EXTRAS) python scripts/check_docs.py

# Mismo orden y comandos que .github/workflows/ci.yml
ci: lint format-check typecheck test docs-check
