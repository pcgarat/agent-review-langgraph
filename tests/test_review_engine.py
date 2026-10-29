"""Frontera del motor de review (025 A2, ADR-0028)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_review.settings import Settings
from agent_review.worker.main import build_runtime


def test_review_engine_por_defecto_es_llm() -> None:
    assert Settings().review_engine == "llm"


def test_review_engine_normaliza_mayusculas_y_espacios() -> None:
    assert Settings(review_engine=" LLM ").review_engine == "llm"
    assert Settings(review_engine="Claude").review_engine == "claude"


def test_review_engine_desconocido_no_construye_settings() -> None:
    with pytest.raises(ValidationError, match="REVIEW_ENGINE"):
        Settings(review_engine="otro")


def test_review_engine_llm_compila_el_grafo_de_hoy(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    worker_main = sys.modules["agent_review.worker.main"]
    llamado = {"grafo": False}

    def fake_build_graph(**kwargs):  # noqa: ANN003
        del kwargs
        llamado["grafo"] = True

        class _Builder:
            def compile(self, checkpointer=None):  # noqa: ANN001
                del checkpointer
                return "compiled"

        return _Builder()

    monkeypatch.setattr(worker_main, "build_graph", fake_build_graph)
    monkeypatch.setattr(worker_main, "build_checkpointer", lambda *a, **k: object())
    monkeypatch.setattr(worker_main, "build_vcs", lambda settings: object())
    monkeypatch.setattr(worker_main, "build_chat", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_progress", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_cancellation", lambda settings: None)
    monkeypatch.setattr(worker_main, "validate_models", lambda registry: None)
    monkeypatch.setattr(worker_main, "build_reviewers", lambda *a, **k: [])

    assert build_runtime(Settings(review_engine="llm", database_url="")) == "compiled"
    assert llamado["grafo"] is True


def test_review_engine_claude_no_cae_al_grafo_de_personas(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from langgraph.checkpoint.memory import InMemorySaver

    worker_main = sys.modules["agent_review.worker.main"]

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("no debe construir el motor llm")

    monkeypatch.setattr(worker_main, "build_graph", boom)
    monkeypatch.setattr(worker_main, "build_reviewers", boom)
    monkeypatch.setattr(worker_main, "build_verifier", boom)
    monkeypatch.setattr(worker_main, "build_fixer", boom)
    monkeypatch.setattr(worker_main, "validate_models", boom)
    monkeypatch.setattr(
        worker_main,
        "PolicyEngine",
        type("PolicyEngine", (), {"from_file": staticmethod(boom)}),
    )
    monkeypatch.setattr(worker_main, "build_checkpointer", lambda *a, **k: InMemorySaver())
    monkeypatch.setattr(worker_main, "build_vcs", lambda settings: object())
    monkeypatch.setattr(worker_main, "build_chat", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_progress", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_cancellation", lambda settings: None)

    graph = build_runtime(Settings(review_engine="claude", database_url=""))
    assert "stamp" in graph.nodes
    assert "verify" not in graph.nodes
    assert "fix_dispatch" not in graph.nodes
