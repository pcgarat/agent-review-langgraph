from __future__ import annotations

from typing import Any

from agent_review import cli
from agent_review.chat_outbox import InMemoryChatOutbox, OutboxChatAdapter
from agent_review.domain.models import (
    ApprovalRequest,
    AutofixClass,
    DecisionOutcome,
    Finding,
    Owner,
    ReviewerResult,
    ReviewRequest,
    Severity,
)
from agent_review.graph.build import build_graph
from agent_review.paused_store import InMemoryPausedStore
from agent_review.policy.engine import PolicyEngine
from agent_review.ports.chat import ChatPort
from agent_review.ports.vcs import InlineAnchor, PullRequestInfo, ReviewMarker
from agent_review.queue import InMemoryReviewQueue
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import DECISION_ACTION, SUBMIT_ACTION
from agent_review.teams_bot.decisions import DecisionSubmitter
from agent_review.teams_bot.ingress import CardIngress
from agent_review.teams_bot.service import TeamsBotService
from agent_review.teams_bot.state import InMemoryCardStore
from agent_review.teams_bot.threads import ChannelContext, InMemoryChannelStore
from agent_review.teams_bot.transport import ChannelTransport
from agent_review.worker.main import process_request


def _submit_enabled(card: dict[str, Any]) -> bool:
    """Si el boton Enviar de la tarjeta esta habilitado (C3)."""
    return bool(
        next(action for action in card["actions"] if action["data"].get("action") == SUBMIT_ACTION)[
            "isEnabled"
        ]
    )


class FakeChannel:
    """Cliente del canal en memoria para el E2E de T9 (sin SDK)."""

    def __init__(self) -> None:
        self.sends: list[Any] = []
        self.replies: list[Any] = []
        self.updates: list[Any] = []
        self._next = 500

    async def send(self, conversation_id: str, payload: Any, *, service_url: str = "") -> str:
        del conversation_id, service_url
        self.sends.append(payload)
        self._next += 1
        return str(self._next)

    async def reply(
        self, conversation_id: str, root_message_id: str, payload: Any, *, service_url: str = ""
    ) -> str:
        del conversation_id, root_message_id, service_url
        self.replies.append(payload)
        self._next += 1
        return str(self._next)

    async def update(
        self, conversation_id: str, message_id: str, payload: Any, *, service_url: str = ""
    ) -> None:
        del conversation_id, message_id, service_url
        self.updates.append(payload)


class _BotHarness:
    """Outbox + bot + ingress + cola de jobs, todos en memoria."""

    def __init__(self, *, paused: InMemoryPausedStore, outbox: InMemoryChatOutbox) -> None:
        self.outbox = outbox
        self.paused = paused
        self.queue = InMemoryReviewQueue()
        self.cards = InMemoryCardStore()
        self.channel = FakeChannel()
        store = InMemoryChannelStore()
        store.remember_context(ChannelContext(conversation_id="chan-1", service_url="https://s"))
        self.transport = ChannelTransport(client=self.channel, store=store)
        self.service = TeamsBotService(
            outbox=outbox, transport=self.transport, paused=paused, poll_s=0.01
        )
        self.ingress = CardIngress(
            paused=paused,
            cards=DecisionCardService(store=self.cards),
            submitter=DecisionSubmitter(paused=paused, queue=self.queue, cards=self.cards),
            transport=self.transport,
        )


class _Vcs:
    """VCS que recuerda lo publicado y lo devuelve como marcas (como Bitbucket)."""

    def __init__(self) -> None:
        self.inlines: list[str] = []
        self.comments: list[str] = []

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo,
            pr_id=pr_id,
            title="Transfer",
            source_branch="f",
            destination_branch="main",
            author="a",
            head_sha="h",
            url="https://example.test/pr",
        )

    def get_diff(self, repo: str, pr_id: int) -> str:
        del repo, pr_id
        return "+x\n"

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
        return [ReviewMarker(id="", body=body) for body in self.inlines + self.comments]


class _StaticReviewer:
    """Persona estatica con un hallazgo que la politica deja pendiente."""

    name = "correctness"

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
        return ReviewerResult(
            findings=[
                Finding(
                    id="f1",
                    title="Saldo puede ser nulo",
                    severity=Severity.P2,
                    file="a.php",
                    line=1,
                    why_it_matters="Riesgo de null dereference",
                    autofix_class=AutofixClass.ADVISORY,
                    owner=Owner.HUMAN,
                    failure_scenario="escenario de prueba",
                    confidence=75,
                    evidence=["a.php:1 $x"],
                )
            ]
        )


class _RecordingChat:
    def __init__(self) -> None:
        self.pending: list[ApprovalRequest] = []

    def notify(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        title: str,
        body: str,
        link: str | None = None,
    ) -> None:
        del repo, pr_id, review_key, title, body, link

    def notify_decision_pending(
        self,
        *,
        repo: str,
        pr_id: int,
        review_key: str,
        approval: ApprovalRequest,
        link: str | None = None,
    ) -> None:
        del repo, pr_id, review_key, link
        self.pending.append(approval)


def _escalating_policy() -> PolicyEngine:
    return PolicyEngine(
        {
            "version": 1,
            "max_autonomy": "fix_with_approval",
            "default_action": "escalate",
            "rules": [],
        }
    )


def _graph(vcs: _Vcs, *, chat: ChatPort | None = None):
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(
        vcs=vcs, reviewers=[_StaticReviewer()], policy=_escalating_policy(), chat=chat
    ).compile(checkpointer=InMemorySaver())


def test_e2e_pausa_decision_reanudacion_publica_sin_duplicar(monkeypatch) -> None:
    """Artefacto -> pausa -> decision (CLI) -> resume (worker) -> inline+resumen.

    Ejercita el camino completo sin `twg` ni red: VCS, inbox y cola falsos, pero
    el grafo, la validacion de la decision y el worker son los reales.
    """
    vcs = _Vcs()
    chat = _RecordingChat()
    paused_store = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    graph = _graph(vcs)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=1, head_sha="abc", pipeline_id="7")

    # 1) El worker procesa la review y el grafo pausa: no se publica nada.
    first = process_request(graph, request, paused=paused_store, chat=chat)
    assert first.paused
    assert vcs.inlines == [] and vcs.comments == []
    assert len(chat.pending) == 1
    assert paused_store.get(request.review_key) is not None

    # 2) La persona decide por el CLI; el CLI encola la reanudacion.
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused_store)
    monkeypatch.setattr(cli, "build_queue", lambda settings: queue)
    assert cli.main(["decide", request.review_key, "--actor", "alice", "--approve", "f1"]) == 0

    # 3) El worker recoge el job de reanudacion y aplica Command(resume=...).
    job = queue.dequeue()
    assert job is not None and job.decision is not None
    resumed = process_request(graph, job, paused=paused_store, chat=chat)

    assert not resumed.paused
    assert len(vcs.inlines) == 1, "se publica el inline una sola vez"
    assert len(vcs.comments) == 1, "y un unico resumen"
    assert "Decision: aprobado · alice" in vcs.inlines[0]
    assert vcs.comments[0].endswith(f"<!-- {request.dedupe_token}:ok -->")
    assert paused_store.get(request.review_key) is None, "el inbox se limpia al decidir"

    # 4) Una reentrega del job (ack perdido) no republica: hilo completado.
    process_request(graph, job, paused=paused_store, chat=chat)
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1


def test_e2e_rechazo_descarta_el_hallazgo_del_inline(monkeypatch) -> None:
    vcs = _Vcs()
    chat = _RecordingChat()
    paused_store = InMemoryPausedStore()
    queue = InMemoryReviewQueue()
    graph = _graph(vcs)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=2, head_sha="def", pipeline_id="8")

    process_request(graph, request, paused=paused_store, chat=chat)
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused_store)
    monkeypatch.setattr(cli, "build_queue", lambda settings: queue)
    assert cli.main(["decide", request.review_key, "--actor", "bob", "--reject", "f1"]) == 0

    job = queue.dequeue()
    assert job is not None
    process_request(graph, job, paused=paused_store, chat=chat)

    assert vcs.inlines == [], "un hallazgo descartado no se anota inline"
    assert vcs.comments, "pero si figura en el resumen"
    assert "Descartado por una persona" in vcs.comments[0]


async def test_e2e_tarjeta_reanuda_y_avisa_sin_duplicar() -> None:
    """T9: review → pausa → outbox → tarjeta → decisión → reanudación → aviso.

    Piezas reales del canal (outbox, TeamsBotService, CardIngress, DecisionSubmitter)
    con transporte y colas falsos. El bot no reanuda: solo publica y encola (C4).
    """
    vcs = _Vcs()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    paused = InMemoryPausedStore()
    bot = _BotHarness(paused=paused, outbox=outbox)
    graph = _graph(vcs, chat=chat)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=3, head_sha="ghi", pipeline_id="9")

    # 1) La review pausa y deja el aviso en la outbox; el PR aún no tiene comentarios.
    first = process_request(graph, request, paused=paused, chat=chat)
    assert first.paused
    assert vcs.inlines == [] and vcs.comments == []
    assert len(outbox) == 1

    # 2) El bot consume el aviso y publica la tarjeta en el hilo del PR.
    assert await bot.service.drain() == (1, False)
    assert len(bot.channel.sends) == 1
    card = bot.channel.sends[0]
    assert card["type"] == "AdaptiveCard"
    assert _submit_enabled(card) is False, "sin marcas, Enviar está bloqueado (C3)"

    # 3) La persona marca y envía por el ingress; el bot encola, no reanuda.
    mark = await bot.ingress.handle(
        {
            "action": DECISION_ACTION,
            "review_key": request.review_key,
            "finding_id": "f1",
            "outcome": DecisionOutcome.APPROVED.value,
        },
        user_id="entra-u1",
        actor="Paco Garat",
    )
    assert mark is not None and mark.card is not None
    assert _submit_enabled(mark.card) is True

    submitted = await bot.ingress.handle(
        {"action": SUBMIT_ACTION, "review_key": request.review_key},
        user_id="entra-u1",
        actor="Paco Garat",
    )
    assert submitted is not None and submitted.notice == "Decision enviada"
    assert len(bot.channel.updates) == 1, "la tarjeta del canal pasa a «decidido» (C11)"

    # 4) El worker recoge el job, publica en el PR y deja el aviso de review.
    job = bot.queue.dequeue()
    assert job is not None and job.decision is not None
    resumed = process_request(graph, job, paused=paused, chat=chat)
    assert not resumed.paused
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1
    assert "Decision: aprobado · Paco Garat" in vcs.inlines[0]
    assert paused.get(request.review_key) is None
    assert len(outbox) == 1, "el aviso de review publicada espera al bot"

    # 5) El bot publica el aviso como reply del mismo hilo; una reentrega no duplica.
    assert await bot.service.drain() == (1, False)
    assert len(bot.channel.sends) == 1, "no se abre otro hilo"
    assert len(bot.channel.replies) == 1
    assert "Review de cqm/emotivoo-v2#3" in bot.channel.replies[0]

    process_request(graph, job, paused=paused, chat=chat)
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1
    assert len(outbox) == 0, "la reentrega no vuelve a encolar el aviso"


async def test_e2e_con_el_bot_caido_el_aviso_se_publica_al_arrancar() -> None:
    """T9: con el bot caído el aviso queda en la outbox y se publica al drenar."""
    vcs = _Vcs()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    paused = InMemoryPausedStore()
    bot = _BotHarness(paused=paused, outbox=outbox)
    graph = _graph(vcs, chat=chat)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=4, head_sha="jkl", pipeline_id="10")

    process_request(graph, request, paused=paused, chat=chat)
    assert len(outbox) == 1
    assert bot.channel.sends == [], "el bot aún no ha consumido"

    assert await bot.service.drain() == (1, False)
    assert len(bot.channel.sends) == 1
    assert bot.channel.sends[0]["type"] == "AdaptiveCard"
    assert len(outbox) == 0


async def test_e2e_aviso_de_pausa_vencida_no_genera_tarjeta(monkeypatch) -> None:
    """T9 / C13: si la pausa ya no está vigente, el bot descarta el aviso sin tarjeta."""
    vcs = _Vcs()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    paused = InMemoryPausedStore()
    bot = _BotHarness(paused=paused, outbox=outbox)
    graph = _graph(vcs, chat=chat)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=5, head_sha="mno", pipeline_id="11")

    process_request(graph, request, paused=paused, chat=chat)
    assert len(outbox) == 1

    # Mientras el bot está caído, alguien decide por el CLI y el worker reanuda.
    monkeypatch.setattr(cli, "build_paused", lambda settings: paused)
    monkeypatch.setattr(cli, "build_queue", lambda settings: bot.queue)
    assert cli.main(["decide", request.review_key, "--actor", "alice", "--approve", "f1"]) == 0
    job = bot.queue.dequeue()
    assert job is not None
    process_request(graph, job, paused=paused, chat=chat)
    assert paused.get(request.review_key) is None
    assert len(outbox) == 2, "queda el aviso de pausa vencido + el de review publicada"

    processed, _ = await bot.service.drain()
    assert processed == 2
    assert outbox.dead_letter_count == 1
    assert "ya no esta vigente" in str(outbox.dead_letters()[0]["reason"])
    # Sin hilo previo: el published abre raíz como texto; la pausa vencida no deja tarjeta.
    assert len(bot.channel.sends) == 1
    assert isinstance(bot.channel.sends[0], str)
    assert "Review de" in bot.channel.sends[0]
    assert bot.channel.replies == []
    assert not any(
        isinstance(payload, dict) and payload.get("type") == "AdaptiveCard"
        for payload in bot.channel.sends
    )


async def test_e2e_cli_con_el_bot_caido_reanuda_con_normalidad(monkeypatch) -> None:
    """T9 / C9: con el bot caído, el CLI reanuda igual; el canal no bloquea el HITL."""
    vcs = _Vcs()
    outbox = InMemoryChatOutbox()
    chat = OutboxChatAdapter(outbox=outbox)
    paused = InMemoryPausedStore()
    bot = _BotHarness(paused=paused, outbox=outbox)
    graph = _graph(vcs, chat=chat)
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=6, head_sha="pqr", pipeline_id="12")

    process_request(graph, request, paused=paused, chat=chat)
    assert len(outbox) == 1
    assert bot.channel.sends == []

    monkeypatch.setattr(cli, "build_paused", lambda settings: paused)
    monkeypatch.setattr(cli, "build_queue", lambda settings: bot.queue)
    assert cli.main(["decide", request.review_key, "--actor", "bob", "--approve", "f1"]) == 0

    job = bot.queue.dequeue()
    assert job is not None
    resumed = process_request(graph, job, paused=paused, chat=chat)
    assert not resumed.paused
    assert len(vcs.inlines) == 1 and len(vcs.comments) == 1
    assert "Decision: aprobado · bob" in vcs.inlines[0]

    # El bot arranca después: descarta la pausa vencida y publica el aviso de review.
    assert await bot.service.drain() == (2, False)
    assert outbox.dead_letter_count == 1
    assert len(bot.channel.sends) == 1
    assert isinstance(bot.channel.sends[0], str)
    assert "Review de cqm/emotivoo-v2#6" in bot.channel.sends[0]
