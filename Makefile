UV ?= uv
EXTRAS ?= dev

.PHONY: install lint format-check typecheck test ci

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

# Mismo orden y comandos que .github/workflows/ci.yml
ci: lint format-check typecheck test
