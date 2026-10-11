UV ?= uv
EXTRAS ?= dev

.PHONY: install lint typecheck test ci

install:
	$(UV) sync --extra $(EXTRAS)

lint:
	$(UV) run --extra $(EXTRAS) ruff check src tests

typecheck:
	$(UV) run --extra $(EXTRAS) mypy

test:
	$(UV) run --extra $(EXTRAS) pytest

# Mismo orden y comandos que .github/workflows/ci.yml
ci: lint typecheck test
