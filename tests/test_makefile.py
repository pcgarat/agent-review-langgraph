from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAKEFILE = _ROOT / "Makefile"


def test_ci_incluye_el_panel_cuando_existe_frontend() -> None:
    """029 R7: un fallo de tipos o del build de la SPA tiene que tumbar `make ci`."""
    text = _MAKEFILE.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("ci:"))
    assert "frontend-ci" in line, "`make ci` no comprueba el panel"

    # Condicionado a que exista `frontend/`: el repo del worker tambien puede
    # construirse sin la SPA y no puede quedarse sin `ci` por eso.
    assert "$(if $(FRONTEND_READY),frontend-ci)" in line
    assert "FRONTEND_READY := $(wildcard $(FRONTEND)/package.json)" in text
    # El build entra: `tsc` por separado no ve lo que Vite resuelve al empaquetar.
    objetivo = text.split("frontend-ci:", 1)[1].split("\n\n", 1)[0]
    assert "npm run build" in objetivo


def test_el_workflow_de_github_repite_la_comprobacion_del_panel() -> None:
    """El CI de verdad instala Node y corre lo mismo que `make ci` (029, R7)."""
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "setup-node" in workflow
    assert "npm ci" in workflow
    assert "npm run typecheck" in workflow
    assert "npm run lint" in workflow
    assert "npm test" in workflow
