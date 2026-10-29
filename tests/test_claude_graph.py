"""Grafo del motor claude (025 G1–G4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from test_graph import FakeVcs, StaticReviewer, make_finding

from agent_review.adapters.claude_artifact import ClaudeArtifactReviewer, ClaudeArtifactStore
from agent_review.adapters.tooling_reviewer import ToolingReviewer
from agent_review.domain.models import (
    DecisionOutcome,
    FileClass,
    Finding,
    FindingAction,
    FindingDecision,
    HitlDecision,
    PublicationMode,
    ReviewCoverage,
    ReviewerResult,
    Severity,
    SkipReason,
)
from agent_review.graph.build import build_approval, build_claude_graph, build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.settings import Settings
from agent_review.trace import read_trace
from agent_review.worker.main import build_runtime

_REPO = "r"
_PR = 1
_SHA = "abc"


class _Chat:
    def __init__(self) -> None:
        self.avisos: list[dict[str, object]] = []

    def notify(self, **kwargs: object) -> None:
        self.avisos.append(kwargs)

    def notify_decision_pending(self, **kwargs: object) -> None:
        del kwargs
        raise AssertionError("el grafo no avisa la pausa")


class _Parcial:
    name = "cobertura"

    def review(self, **kwargs: object) -> ReviewerResult:
        del kwargs
        return ReviewerResult(
            findings=[],
            coverage=ReviewCoverage(
                files_total=1,
                skipped={"src/a.php": SkipReason.FAILED},
                file_class={"src/a.php": FileClass.CODE},
            ),
        )


def _store(tmp_path: Path) -> ClaudeArtifactStore:
    return ClaudeArtifactStore(root=tmp_path)


def _guardar(store: ClaudeArtifactStore, findings: list[Finding], *, sha: str = _SHA) -> None:
    body = json.dumps([item.model_dump(mode="json") for item in findings])
    assert store.save(body, repo=_REPO, pr_id=_PR, head_sha=sha)


def _grafo(
    tmp_path: Path,
    reviewers: list[object],
    *,
    chat: _Chat | None = None,
    vcs: FakeVcs | None = None,
):
    store = _store(tmp_path)
    personas = [ClaudeArtifactReviewer(store), *reviewers]
    compiled = build_claude_graph(
        vcs=vcs or FakeVcs(anchors={"tests/FooTest.php": _ancla()}),
        reviewers=personas,  # type: ignore[arg-type]
        chat=chat,
    ).compile(checkpointer=InMemorySaver())
    return store, compiled


def _ancla():
    from agent_review.ports.vcs import InlineAnchor

    return InlineAnchor(path="tests/FooTest.php", line=3)


def _config() -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": f"{_REPO}:{_PR}:{_SHA}"}}


def _invoke(graph, store: ClaudeArtifactStore, findings: list[Finding]):
    _guardar(store, findings)
    return graph.invoke(
        {"request": {"repo": _REPO, "pr_id": _PR, "head_sha": _SHA}, "findings": []},
        config=_config(),
    )


def _pendientes(result: dict[str, object]) -> list[str]:
    interrupts = result["__interrupt__"]
    approval = interrupts[0].value  # type: ignore[index, attr-defined]
    return [item.finding_id for item in approval.pending]


def _decision(items: list[tuple[str, DecisionOutcome]]) -> HitlDecision:
    return HitlDecision(
        review_key=f"{_REPO}:{_PR}:{_SHA}",
        actor="ana",
        items=[FindingDecision(finding_id=fid, outcome=outcome) for fid, outcome in items],
    )


def test_las_aristas_de_claude_no_incluyen_verify_ni_fix() -> None:
    builder = build_claude_graph(
        vcs=FakeVcs(),
        reviewers=[ClaudeArtifactReviewer(ClaudeArtifactStore(root="."))],
    )
    edges = set(builder.edges)
    assert ("merge", "stamp") in edges
    assert ("stamp", "hitl") in edges
    assert ("hitl", "publish") in edges
    assert ("publish", "notify") in edges
    assert "verify" not in builder.nodes
    assert "fix_dispatch" not in builder.nodes
    assert "policy" not in builder.nodes


def test_el_grafo_llm_sigue_con_verify_y_fix_dispatch() -> None:
    builder = build_graph(
        vcs=FakeVcs(),
        reviewers=[StaticReviewer("r", [])],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
    )
    edges = set(builder.edges)
    assert ("merge", "verify") in edges
    assert ("fix_dispatch", "publish") in edges
    assert "verify" in builder.nodes
    assert "fix_dispatch" in builder.nodes


def test_el_motor_claude_no_pide_el_artefacto_estatico(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    perfil = tmp_path / "perfil.yaml"
    perfil.write_text("repo: cqm/demo\nchecks:\n  typecheck: make stan\n", encoding="utf-8")
    sin = tmp_path / "sin.yaml"
    sin.write_text("repo: cqm/demo\n", encoding="utf-8")
    worker_main = sys.modules["agent_review.worker.main"]
    capturado: dict[str, list[str]] = {}

    def fake_build_claude_graph(**kwargs):  # noqa: ANN003
        capturado["nombres"] = [reviewer.name for reviewer in kwargs["reviewers"]]

        class _Builder:
            def compile(self, checkpointer=None):  # noqa: ANN001
                del checkpointer
                return "compiled"

        return _Builder()

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("no debe construir el motor llm")

    monkeypatch.setattr(worker_main, "build_claude_graph", fake_build_claude_graph)
    monkeypatch.setattr(worker_main, "build_graph", boom)
    monkeypatch.setattr(worker_main, "build_reviewers", boom)
    monkeypatch.setattr(worker_main, "build_verifier", boom)
    monkeypatch.setattr(worker_main, "build_checkpointer", lambda *a, **k: object())
    monkeypatch.setattr(worker_main, "build_vcs", lambda settings: object())
    monkeypatch.setattr(worker_main, "build_chat", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_progress", lambda settings: None)
    monkeypatch.setattr(worker_main, "build_cancellation", lambda settings: None)

    build_runtime(
        Settings(review_engine="claude", database_url="", repo_profile_config=str(perfil))
    )
    assert capturado["nombres"] == ["claude"]

    build_runtime(Settings(review_engine="claude", database_url="", repo_profile_config=str(sin)))
    assert capturado["nombres"] == ["claude"]


def test_l0_quita_la_cita_fuera_del_diff_antes_de_la_pausa(tmp_path: Path) -> None:
    store, graph = _grafo(tmp_path, [])
    dentro = make_finding("Cita dentro").model_copy(update={"id": "dentro"})
    fuera = make_finding("Cita fuera").model_copy(
        update={
            "id": "fuera",
            "evidence": ["tests/FooTest.php:3 esta cita no esta en el diff"],
        }
    )
    result = _invoke(graph, store, [dentro, fuera])
    assert _pendientes(result) == ["dentro"]
    descartes = graph.get_state(_config()).values["discarded"]
    assert [(item.stage, item.id) for item in descartes] == [("l0", "fuera")]


def test_la_persona_estatica_entra_en_la_misma_pausa(tmp_path: Path) -> None:
    phpstan = json.dumps(
        {
            "files": {
                "tests/FooTest.php": {
                    "messages": [
                        {"message": "Call to an undefined method.", "line": 3, "level": "error"}
                    ]
                }
            }
        }
    )
    store, graph = _grafo(tmp_path, [ToolingReviewer(phpstan_json=phpstan)])
    cli = make_finding("Hallazgo del CLI").model_copy(update={"id": "cli"})
    result = _invoke(graph, store, [cli])
    pendientes = _pendientes(result)
    assert "cli" in pendientes
    assert any(item.startswith("standards-") for item in pendientes)
    assert len(pendientes) == 2
    # Dos ramas en el mismo paso: las escrituras no dicen de quién es cada una.
    trace = read_trace(graph.checkpointer, _config()["configurable"]["thread_id"])
    assert trace is not None
    assert [node.summary for node in trace.nodes if node.name == "persona"] == [None, None]


def test_artefacto_vacio_no_pausa_ni_escribe_y_avisa(tmp_path: Path) -> None:
    chat = _Chat()
    vcs = FakeVcs()
    store, graph = _grafo(tmp_path, [], chat=chat, vcs=vcs)
    _guardar(store, [])
    result = graph.invoke(
        {"request": {"repo": _REPO, "pr_id": _PR, "head_sha": _SHA}, "findings": []},
        config=_config(),
    )
    assert "__interrupt__" not in result
    assert vcs.comments == []
    assert vcs.inlines == []
    assert len(chat.avisos) == 1
    assert "0 hallazgos" not in str(chat.avisos[0]["body"])


def test_todo_dropped_no_escribe_en_el_pr_aunque_la_cobertura_sea_parcial(tmp_path: Path) -> None:
    chat = _Chat()
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [_Parcial()], chat=chat, vcs=vcs)
    hallazgo = make_finding("No publicar").model_copy(update={"id": "uno"})
    _invoke(graph, store, [hallazgo])
    graph.invoke(Command(resume=_decision([("uno", DecisionOutcome.DROPPED)])), config=_config())
    assert vcs.comments == []
    assert vcs.inlines == []
    assert "0 hallazgos" not in str(chat.avisos[-1]["body"])


def test_sin_artefacto_la_persona_estatica_no_deja_la_review_incompleta(
    tmp_path: Path,
) -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [ToolingReviewer()], vcs=vcs)
    hallazgo = make_finding("No publicar").model_copy(update={"id": "uno"})
    _invoke(graph, store, [hallazgo])
    graph.invoke(Command(resume=_decision([("uno", DecisionOutcome.DROPPED)])), config=_config())
    assert vcs.inlines == []
    assert vcs.comments == []


def test_expired_no_escribe_en_el_pr(tmp_path: Path) -> None:
    chat = _Chat()
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [], chat=chat, vcs=vcs)
    hallazgo = make_finding("No publicar").model_copy(update={"id": "uno"})
    _invoke(graph, store, [hallazgo])
    graph.invoke(Command(resume=_decision([("uno", DecisionOutcome.EXPIRED)])), config=_config())
    assert vcs.comments == []
    assert vcs.inlines == []
    assert "No se escribió" in str(chat.avisos[-1]["body"])


def test_el_aviso_cuenta_lo_publicado(tmp_path: Path) -> None:
    chat = _Chat()
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [], chat=chat, vcs=vcs)
    hallazgo = make_finding("Publicar").model_copy(update={"id": "uno"})
    _invoke(graph, store, [hallazgo])
    graph.invoke(Command(resume=_decision([("uno", DecisionOutcome.APPROVED)])), config=_config())
    body = str(chat.avisos[-1]["body"])
    assert "Se publicaron 1" in body
    assert "requieren decision" not in body


def test_solo_bloqueantes_deja_el_resto_fuera_del_resumen(tmp_path: Path) -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [], vcs=vcs)
    hallazgos = [
        make_finding("Bloqueante cero", severity=Severity.P0).model_copy(update={"id": "p0"}),
        make_finding("Bloqueante uno", severity=Severity.P1).model_copy(update={"id": "p1"}),
        make_finding("Aviso menor", severity=Severity.P2).model_copy(update={"id": "p2"}),
    ]
    _invoke(graph, store, hallazgos)
    graph.invoke(
        Command(
            resume=_decision(
                [
                    ("p0", DecisionOutcome.APPROVED),
                    ("p1", DecisionOutcome.APPROVED),
                    ("p2", DecisionOutcome.DROPPED),
                ]
            )
        ),
        config=_config(),
    )
    assert vcs.comments
    body = vcs.comments[0][2]
    assert "Bloqueante cero" in body
    assert "Bloqueante uno" in body
    assert "Aviso menor" not in body
    assert "descartado" not in body.lower()


def test_el_sello_es_publish_y_no_degrada_la_severidad(tmp_path: Path) -> None:
    store, graph = _grafo(tmp_path, [])
    hallazgo = make_finding("Cita dentro", severity=Severity.P2).model_copy(update={"id": "dentro"})
    result = _invoke(graph, store, [hallazgo])
    decisions = graph.get_state(_config()).values["decisions"]
    assert decisions[0].action is FindingAction.PUBLISH
    assert decisions[0].publication is PublicationMode.FULL
    assert decisions[0].severity is Severity.P2
    values = graph.get_state(_config()).values
    assert values["silence_without_choice"] is True
    assert values["pause_kind"] == "publication"
    rebuilt = build_approval(values)
    assert rebuilt is not None and rebuilt.pause_kind == "publication"
    assert result["__interrupt__"][0].value.pause_kind == "publication"  # type: ignore[index, attr-defined]


def test_la_traza_cuenta_lo_que_hizo_cada_paso(tmp_path: Path) -> None:
    vcs = FakeVcs(anchors={"tests/FooTest.php": _ancla()})
    store, graph = _grafo(tmp_path, [], vcs=vcs)
    dentro = make_finding("Cita dentro").model_copy(update={"id": "dentro"})
    fuera = make_finding("Cita fuera").model_copy(
        update={"id": "fuera", "evidence": ["tests/FooTest.php:3 no esta en el diff"]}
    )
    _invoke(graph, store, [dentro, fuera])
    graph.invoke(
        Command(resume=_decision([("dentro", DecisionOutcome.APPROVED)])), config=_config()
    )

    trace = read_trace(graph.checkpointer, _config()["configurable"]["thread_id"])

    assert trace is not None
    by_name = {node.name: node for node in trace.nodes}
    assert by_name["scope"].summary == {"files": 1, "ignored": 0}
    assert by_name["persona"].summary == {"returned": 2, "contract": 0, "l0": 1, "kept": 1}
    assert [(item.id, item.reason) for item in by_name["persona"].discarded] == [
        ("fuera", "la cita no aparece en el diff")
    ]
    assert by_name["merge"].summary == {"before": 1, "after": 1}
    assert by_name["stamp"].summary == {"publish": 1, "other": 0}
    assert by_name["publish"].summary == {"published": 1, "inline": 1}
    assert by_name["hitl"].summary is None


def test_la_traza_de_un_hilo_sin_descartes_no_falla(tmp_path: Path) -> None:
    store, graph = _grafo(tmp_path, [])
    _invoke(graph, store, [])

    trace = read_trace(graph.checkpointer, _config()["configurable"]["thread_id"])

    assert trace is not None
    persona = next(node for node in trace.nodes if node.name == "persona")
    assert persona.summary == {"returned": 0, "contract": 0, "l0": 0, "kept": 0}
    assert persona.discarded == []
