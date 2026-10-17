from __future__ import annotations

import subprocess

import httpx
import pytest
from langgraph.types import RetryPolicy

from agent_review.adapters.llm_reviewer import LlmReviewer
from agent_review.adapters.twg_vcs import TwgError, TwgVcsAdapter
from agent_review.domain.models import (
    AutofixClass,
    Finding,
    Owner,
    ReviewerResult,
    Severity,
)
from agent_review.errors import PermanentError, TransientError, is_transient
from agent_review.graph.build import build_graph
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.llm import LlmError, LlmReply, LlmTransientError
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo

# ---------------------------------------------------------------------------
# Clasificacion de errores
# ---------------------------------------------------------------------------


def _http_status(code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        str(code), request=httpx.Request("GET", "https://x.test"), response=httpx.Response(code)
    )


def test_is_transient_reintenta_solo_lo_recuperable() -> None:
    transitorios = [
        TwgError("twg fallo (1): boom"),
        TransientError("explicito"),
        httpx.ConnectError("sin ruta"),
        httpx.ReadTimeout("lento"),
        _http_status(503),
        _http_status(429),
        subprocess.TimeoutExpired(cmd="twg", timeout=1),
        TimeoutError("agotado"),
        ConnectionError("caida"),
    ]
    permanentes = [
        ValueError("bug"),
        TypeError("bug"),
        KeyError("bug"),
        PermanentError("explicito"),
        _http_status(404),
        _http_status(400),
    ]
    assert [is_transient(exc) for exc in transitorios] == [True] * len(transitorios)
    assert [is_transient(exc) for exc in permanentes] == [False] * len(permanentes)


def test_el_default_de_langgraph_no_reintentaria_los_errores_del_repo() -> None:
    # Por que el grafo NO usa `default_retry_on`: no reintenta RuntimeError (de la
    # que heredan TwgError/LlmError) ni TimeoutError, y si un 500 desconocido.
    from langgraph.types import default_retry_on

    twg = TwgError("twg fallo (1): boom")
    assert is_transient(twg) is True
    assert default_retry_on(twg) is False
    assert default_retry_on(TimeoutError("agotado")) is False


# ---------------------------------------------------------------------------
# Techo de la llamada a twg (sin el, un twg colgado bloquea el nodo para siempre)
# ---------------------------------------------------------------------------


def test_twg_pasa_timeout_al_subprocess(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="r", twg_bin="twg", timeout_s=7.5)
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "diff"
        stderr = ""

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr("agent_review.adapters.twg_vcs.subprocess.run", fake_run)
    adapter.get_diff("cqm/r", 1)
    assert captured["timeout"] == 7.5


def test_twg_colgado_es_un_fallo_transitorio(monkeypatch) -> None:
    adapter = TwgVcsAdapter(workspace="cqm", repo="r")

    def boom(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("agent_review.adapters.twg_vcs.subprocess.run", boom)
    with pytest.raises(TwgError, match="excedio"):
        adapter.get_diff("cqm/r", 1)
    assert is_transient(TwgError("x")) is True


# ---------------------------------------------------------------------------
# Reintento de nodo end-to-end
# ---------------------------------------------------------------------------


def _finding() -> Finding:
    return Finding(
        id="f1",
        title="Falta test",
        severity=Severity.P3,
        file="src/foo.py",
        line=3,
        why_it_matters="No cubre el caso borde",
        autofix_class=AutofixClass.GATED_AUTO,
        owner=Owner.DOWNSTREAM_RESOLVER,
        failure_scenario="escenario de prueba",
        confidence=75,
        evidence=["src/foo.py:3 falta assert"],
    )


class FlakyVcs:
    """VCS que falla las primeras `fail_diff_times` veces al pedir el diff."""

    def __init__(
        self,
        error: Exception,
        *,
        fail_diff_times: int = 0,
        fail_summary_times: int = 0,
    ) -> None:
        self._error = error
        self._fail_diff = fail_diff_times
        self._fail_summary = fail_summary_times
        self.diff_calls = 0
        self.inlines: list[str] = []
        self.comments: list[str] = []
        self.markers: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="t",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="https://x.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        self.diff_calls += 1
        if self.diff_calls <= self._fail_diff:
            raise self._error
        from diff_helpers import added_lines_diff

        return added_lines_diff(("src/foo.py", 3), text="amor")

    def resolve_inline_anchor(
        self, repo: str, pr_id: int, *, path: str, text: str
    ) -> InlineAnchor | None:
        del repo, pr_id, text
        return InlineAnchor(path=path, line=3)

    def post_inline_comment(
        self, repo: str, pr_id: int, *, path: str, line: int, body: str, from_line=None
    ) -> None:
        del repo, pr_id, line, from_line
        self.inlines.append(path)
        self.markers.append(body)

    def post_comment(self, repo: str, pr_id: int, body: str) -> None:
        del repo, pr_id
        if self._fail_summary > 0:
            self._fail_summary -= 1
            raise self._error
        self.comments.append(body)
        self.markers.append(body)

    def post_task(self, repo: str, pr_id: int, body: str) -> None:  # pragma: no cover
        del repo, pr_id, body

    def list_review_markers(self, repo: str, pr_id: int) -> list:
        del repo, pr_id
        return [type("M", (), {"id": str(i), "body": b})() for i, b in enumerate(self.markers)]


class CountingReviewer:
    name = "standards"

    def __init__(self, *, name: str = "standards", fail_times: int = 0) -> None:
        self.name = name
        self.calls = 0
        self._fail_times = fail_times

    def review(
        self,
        *,
        repo: str,
        pr_id: int,
        diff: str,
        head_sha: str = "",
        review_key: str = "",
    ) -> ReviewerResult:
        del repo, pr_id, diff, head_sha
        self.calls += 1
        if self.calls <= self._fail_times:
            raise TwgError("twg fallo (1): transitorio")
        return ReviewerResult(findings=[_finding()])


def _graph(vcs, reviewer):  # noqa: ANN001
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    return build_graph(vcs=vcs, reviewers=[reviewer], policy=policy).compile()


def test_el_nodo_scope_reintenta_un_fallo_transitorio() -> None:
    # Un twg que falla una vez no debe tumbar la review: el nodo reintenta dentro
    # de la misma invocacion, sin sacar el job de la cola.
    vcs = FlakyVcs(TwgError("twg fallo (1): 500"), fail_diff_times=1)
    graph = _graph(vcs, CountingReviewer())

    result = graph.invoke(
        {"request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"}, "findings": []}
    )

    assert vcs.diff_calls == 2, "debe reintentar el diff una vez"
    assert result["merged"], "el reintento debe completar la review"
    assert vcs.inlines == ["src/foo.py"]


def test_el_nodo_no_reintenta_un_fallo_permanente() -> None:
    # Un ValueError es un bug: reintentarlo solo lo repite.
    vcs = FlakyVcs(ValueError("bug de programacion"), fail_diff_times=99)
    graph = _graph(vcs, CountingReviewer())

    with pytest.raises(ValueError):
        graph.invoke(
            {"request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"}, "findings": []}
        )
    assert vcs.diff_calls == 1


def test_reintento_de_persona_no_duplica_hallazgos() -> None:
    # El reducer de `findings` acumularia si LangGraph no limpiara las escrituras
    # del intento fallido: la persona que falla y se recupera no debe duplicar.
    vcs = FlakyVcs(TwgError("twg fallo (1): 500"))
    reviewer = CountingReviewer(fail_times=1)
    graph = _graph(vcs, reviewer)

    result = graph.invoke(
        {"request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"}, "findings": []}
    )

    assert reviewer.calls == 2, "el fallo transitorio de la persona se reintenta"
    assert len(result["merged"]) == 1, "el reintento no puede duplicar el hallazgo"
    assert len(vcs.inlines) == 1


def test_reintento_de_publish_no_repite_lo_ya_publicado() -> None:
    # Fallo transitorio al publicar el resumen: el reintento del nodo vuelve a
    # entrar en `publish`, que relee las marcas y no repite el inline ya escrito.
    vcs = FlakyVcs(TwgError("twg fallo (1): 500"), fail_summary_times=1)
    graph = _graph(vcs, CountingReviewer())

    graph.invoke(
        {"request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"}, "findings": []}
    )

    assert len(vcs.inlines) == 1, "el inline ya publicado no se repite tras el reintento"
    assert len(vcs.comments) == 1, "el resumen si se publica en el reintento"


def test_solo_los_nodos_puros_o_de_pausa_quedan_sin_politica() -> None:
    # Invariante: `merge` y `policy` son deterministas y `hitl`/`fix_wait` no deben
    # reintentar (re-ejecutarian la pausa). Cualquier otro nodo llama a fuera y la
    # lleva, asi que anadir un nodo con efectos sin politica rompe este test a proposito.
    policy = PolicyEngine.from_file("config/autonomy.yaml")
    builder = build_graph(
        vcs=FlakyVcs(TwgError("x")), reviewers=[CountingReviewer()], policy=policy
    )

    sin_politica = {name for name, spec in builder.nodes.items() if spec.retry_policy is None}
    assert sin_politica == {"merge", "policy", "hitl", "fix_wait"}


# ---------------------------------------------------------------------------
# Persona LLM: reintento de fallos transitorios y degradacion al agotarlos
# ---------------------------------------------------------------------------


class _FlakyLlm:
    def __init__(self, error: Exception, *, fail_times: int) -> None:
        self._error = error
        self._fail = fail_times
        self.calls = 0

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode=None,
        response_schema=None,
    ) -> LlmReply:
        del system, prompt, max_tokens, json_mode, response_schema
        self.calls += 1
        if self.calls <= self._fail:
            raise self._error
        return LlmReply(text='{"findings": []}')


def _fast_retry() -> RetryPolicy:
    return RetryPolicy(max_attempts=3, initial_interval=0.001, jitter=False, retry_on=is_transient)


def _llm_graph(llm: _FlakyLlm):  # noqa: ANN202
    vcs = FlakyVcs(TwgError("x"))
    graph = build_graph(
        vcs=vcs,
        reviewers=[CountingReviewer(), LlmReviewer(llm=llm)],
        policy=PolicyEngine.from_file("config/autonomy.yaml"),
        retry_policy=_fast_retry(),
    ).compile()
    return graph, vcs


def _run(graph) -> dict:  # noqa: ANN001
    return graph.invoke(
        {"request": {"repo": "cqm/emotivoo-v2", "pr_id": 7, "head_sha": "abc"}, "findings": []}
    )


def test_llm_reviewer_reintenta_el_transitorio_en_el_lote_y_degrada_el_permanente() -> None:
    # Las personas solo reciben bloques `diff --git` (los deja pasar `scope`).
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x\n"
    # 021 R14: un transitorio se reintenta dentro del lote; si se recupera, no sube.
    flaky = _FlakyLlm(LlmTransientError("503"), fail_times=1)
    transient = LlmReviewer(llm=flaky)
    assert transient.review(repo="r", pr_id=1, diff=diff).coverage.reviewed == ["a.py"]
    assert flaky.calls == 2

    permanent = LlmReviewer(llm=_FlakyLlm(LlmError("401"), fail_times=1))
    assert permanent.review(repo="r", pr_id=1, diff=diff).findings == []


def test_el_nodo_persona_reintenta_un_fallo_transitorio_del_llm() -> None:
    llm = _FlakyLlm(LlmTransientError("503"), fail_times=2)
    graph, _vcs = _llm_graph(llm)

    _run(graph)

    assert llm.calls == 3, "dos fallos transitorios y el tercer intento responde"


def test_llm_caido_tras_agotar_intentos_no_tumba_la_review() -> None:
    # Agotados los intentos, la persona LLM degrada a 0 hallazgos: la review se
    # publica con los de la estatica en vez de acabar en la DLQ.
    llm = _FlakyLlm(LlmTransientError("503"), fail_times=10**6)
    graph, vcs = _llm_graph(llm)

    result = _run(graph)

    assert llm.calls == 3
    assert [f.id for f in result["merged"]] == ["f1"], "los hallazgos de la estatica siguen"
    assert len(vcs.comments) == 1
