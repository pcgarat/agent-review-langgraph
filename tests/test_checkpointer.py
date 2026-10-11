from __future__ import annotations

import pytest

from reviews.checkpointer import build_checkpointer, build_serde
from reviews.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from reviews.graph.build import build_graph
from reviews.policy.engine import PolicyEngine
from reviews.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from reviews.worker.main import process_request


class _MemoryVcs:
    """VCS falso que recuerda lo publicado, como haria Bitbucket."""

    def __init__(self) -> None:
        self.inlines: list[str] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="t",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="u",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return "diff --git a/a.php b/a.php\n+x\n"

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, int, int]]:
        del repo, pr_id
        return [("a.php", 1, 0)]

    def resolve_inline_anchor(self, repo: str, pr_id: int, *, path: str, text: str):
        del repo, pr_id, text
        return InlineAnchor(path=path, line=1)

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, path, line, from_line
        self.inlines.append(body)

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        self.comments.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        del repo, pr_id, body

    def list_review_markers(self, repo: str, pr_id: int) -> list[ReviewMarker]:
        del repo, pr_id
        # Devuelve lo publicado por este VCS, como haria Bitbucket: es lo que
        # evita duplicar un inline ya escrito cuando el resumen fallo despues.
        return [ReviewMarker(id="", body=body) for body in self.inlines + self.comments]


class _OneFinding:
    name = "standards"

    def review(self, *, repo: str, pr_id: int, diff: str, head_sha: str = "") -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        return ReviewerResult(
            findings=[
                Finding(
                    id="f1",
                    title="T",
                    severity=Severity.P3,
                    file="a.php",
                    line=1,
                    why_it_matters="w",
                    autofix_class=AutofixClass.ADVISORY,
                    owner=Owner.HUMAN,
                    requires_verification=False,
                    confidence=75,
                    evidence=["a.php:1 x"],
                )
            ]
        )


def _graph(vcs: _MemoryVcs):
    saver = build_checkpointer("")  # memoria, sin Postgres
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    return build_graph(vcs=vcs, reviewers=[_OneFinding()], policy=policy).compile(
        checkpointer=saver
    )


def test_serde_no_depende_del_msgpack_permisivo() -> None:
    # Si el serializer no tuviera allowlist, la deserializacion avisaria. Con la
    # allowlist explicita el roundtrip del estado de dominio es silencioso.
    serde = build_serde()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    payload = serde.dumps_typed(request)
    assert serde.loads_typed(payload) == request


def test_thread_id_estable_por_ejecucion() -> None:
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")
    # Mismo request y un reintento (attempts distinto) comparten hilo.
    retry = request.model_copy(update={"attempts": 1})
    assert request.thread_id == retry.thread_id == "r:1:abc:7"


def test_reentrega_del_mismo_thread_no_republica() -> None:
    vcs = _MemoryVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    process_request(graph, request)
    assert len(vcs.inlines) == 1
    assert len(vcs.comments) == 1

    # Reentrega identica (p.ej. ack perdido): no debe duplicar.
    process_request(graph, request.model_copy(update={"attempts": 1}))
    assert len(vcs.inlines) == 1
    assert len(vcs.comments) == 1


class _MidRunFailVcs(_MemoryVcs):
    """Falla el resumen la primera vez, tras haber publicado el inline."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_summary = True

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        if self.fail_next_summary:
            self.fail_next_summary = False
            raise RuntimeError("resumen caido (transitorio)")
        super().post_comment(repo, pr_id, body)


def test_fallo_a_media_ejecucion_deja_el_hilo_incompleto() -> None:
    # La premisa del reintento: un nodo que falla a media ejecucion NO deja el
    # hilo "completado" (queda `next=('publish',)`), asi que el checkpointer no
    # puede saltarse la re-ejecucion. El job ya salio de la cola con BLPOP: solo
    # `_consume` impide perderlo.
    vcs = _MidRunFailVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    with pytest.raises(RuntimeError):
        process_request(graph, request)

    config = {"configurable": {"thread_id": request.thread_id}}
    snapshot = graph.get_state(config)
    assert snapshot.next, "el hilo queda con el nodo pendiente, no completado"
    assert len(vcs.inlines) == 1
    assert vcs.comments == []


def test_reintento_con_checkpointer_completa_sin_duplicar_el_inline() -> None:
    # End-to-end del fallo parcial: el reintento re-ejecuta el nodo pendiente,
    # completa el resumen que faltaba y no repite el inline ya publicado (lo
    # detecta por la marca del commit leida del VCS).
    vcs = _MidRunFailVcs()
    graph = _graph(vcs)
    request = ReviewRequest(repo="r", pr_id=1, head_sha="abc", pipeline_id="7")

    with pytest.raises(RuntimeError):
        process_request(graph, request)

    process_request(graph, request.model_copy(update={"attempts": 1}))

    assert len(vcs.inlines) == 1, "no debe duplicar el inline ya publicado"
    assert len(vcs.comments) == 1, "debe escribir el resumen que faltaba"
    snapshot = graph.get_state({"configurable": {"thread_id": request.thread_id}})
    assert not snapshot.next, "el hilo queda completado"
