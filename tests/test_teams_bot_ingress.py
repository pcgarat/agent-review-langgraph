"""T7 (iniciativa 013): ingress del Bot Framework y traduccion de acciones.

Dos niveles, a proposito:

- **Reglas del ingress** (`CardIngress`): marcar, enviar, avisos y que un fallo de
  infraestructura no se convierta en un `500` para Teams. Con fakes, sin SDK.
- **Wiring** (`sdk_handlers`): que los handlers del SDK queden registrados, que el
  `Action.Execute` real llegue al ingress y devuelva la tarjeta repintada, y que el
  canal se capture de cualquier actividad (C14). La ultima prueba corre el pipeline
  real del SDK sobre FastAPI (`POST /api/messages`), que es donde se demuestra que el
  JWT se exige y que la accion encola de verdad.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent_review.domain.models import (
    ApprovalRequest,
    DecisionOutcome,
    FindingAction,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue
from agent_review.teams_bot.card_service import DecisionCardService
from agent_review.teams_bot.cards import DECISION_ACTION, SUBMIT_ACTION
from agent_review.teams_bot.decisions import DecisionSubmitter
from agent_review.teams_bot.ingress import CardIngress
from agent_review.teams_bot.state import InMemoryCardStore
from agent_review.teams_bot.threads import ChannelContext, InMemoryChannelStore
from agent_review.teams_bot.transport import ChannelTransport


class FakeChannel:
    """Cliente del canal en memoria: registra lo publicado."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sends: list[Any] = []
        self.replies: list[Any] = []
        self.updates: list[Any] = []
        self.fail = fail
        self._next = 400

    async def send(self, conversation_id: str, payload: Any, *, service_url: str = "") -> str:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.sends.append(payload)
        self._next += 1
        return str(self._next)

    async def reply(
        self, conversation_id: str, root_message_id: str, payload: Any, *, service_url: str = ""
    ) -> str:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.replies.append(payload)
        self._next += 1
        return str(self._next)

    async def update(
        self, conversation_id: str, message_id: str, payload: Any, *, service_url: str = ""
    ) -> None:
        if self.fail:
            raise ConnectionError("Teams caido")
        self.updates.append(payload)


def _pending(finding_id: str = "f1") -> PendingDecision:
    return PendingDecision(
        finding_id=finding_id,
        proposed_action=FindingAction.FIX_WITH_APPROVAL,
        reason="Tema protegido",
        matched_rule="rule:3",
        policy_version="1",
        title="Escritura sin comprobar",
        file="src/Pay.php",
        line=12,
        severity=Severity.P1,
    )


def _approval(review_key: str, *ids: str) -> ApprovalRequest:
    return ApprovalRequest(
        review_key=review_key,
        repo="cqm/emotivoo-v2",
        pr_id=7,
        head_sha="abc",
        pending=[_pending(fid) for fid in (ids or ("f1",))],
    )


class _Harness:
    """Piezas del ingress con stores en memoria y contadores observables."""

    def __init__(
        self,
        *,
        paused: InMemoryPausedStore,
        channel: FakeChannel | None = None,
        approval: ApprovalRequest | None = None,
    ) -> None:
        self.paused = paused
        self.approval = approval or _approval("r:1:abc")
        self.queue = InMemoryReviewQueue()
        self.marks = InMemoryCardStore()
        self.channel = channel or FakeChannel()
        self.store = InMemoryChannelStore()
        self.store.remember_context(ChannelContext(conversation_id="c", service_url="s"))
        self.transport = ChannelTransport(client=self.channel, store=self.store)
        self.ingress = CardIngress(
            paused=paused,
            cards=DecisionCardService(store=self.marks),
            submitter=DecisionSubmitter(paused=paused, queue=self.queue, cards=self.marks),
            transport=self.transport,
        )


def _harness(*pending: str) -> tuple[_Harness, str]:
    """Ingress con una review pausada de verdad en el inbox."""
    paused = InMemoryPausedStore()
    request = ReviewRequest(repo="cqm/emotivoo-v2", pr_id=7, head_sha="abc")
    approval = _approval(request.review_key, *pending)
    paused.pause(request, approval)
    return _Harness(paused=paused, approval=approval), request.review_key


def _data(action: str, review_key: str, **extra: str) -> dict[str, str]:
    return {"action": action, "review_key": review_key, **extra}


# --- Reglas del ingress -------------------------------------------------------


async def test_marcar_un_hallazgo_devuelve_la_tarjeta_con_esa_marca() -> None:
    harness, review_key = _harness()

    update = await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )

    assert update is not None and update.card is not None
    assert harness.marks.marks(review_key, user_id="u1") == {"f1": DecisionOutcome.APPROVED}
    assert harness.queue.dequeue() is None, "marcar no encola nada"


async def test_el_estado_de_la_marca_es_por_usuario() -> None:
    """C8: dos personas marcan en paralelo sin pisarse."""
    harness, review_key = _harness()

    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="rejected"),
        user_id="u1",
        actor="Ana",
    )
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="noted"),
        user_id="u2",
        actor="Luis",
    )

    assert harness.marks.marks(review_key, user_id="u1") == {"f1": DecisionOutcome.REJECTED}
    assert harness.marks.marks(review_key, user_id="u2") == {"f1": DecisionOutcome.NOTED}


async def test_enviar_una_decision_completa_encola_y_repinta_la_tarjeta() -> None:
    harness, review_key = _harness()
    # La tarjeta ya estaba publicada en el canal por el servicio (T5): el repintado
    # es una edicion de ese mensaje, no una publicacion nueva (C7).
    approval = harness.approval
    await harness.transport.post_card(
        review_key, repo=approval.repo, pr_id=approval.pr_id, card={"type": "AdaptiveCard"}
    )
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, review_key), user_id="u1", actor="Ana"
    )

    assert update is not None and update.notice == "Decision enviada"
    enqueued = harness.queue.dequeue()
    assert enqueued is not None and enqueued.decision is not None
    assert enqueued.decision.actor == "Ana"
    assert harness.marks.final(review_key) == "Ana"
    # El canal ve la tarjeta cerrada (C7/C11), editando el mensaje que ya existia.
    assert len(harness.channel.updates) == 1
    assert len(harness.channel.sends) == 1, "solo la publicacion inicial del aviso"
    assert harness.channel.replies == []


async def test_una_decision_incompleta_no_encola_y_avisa() -> None:
    harness, review_key = _harness("f1", "f2")
    # Solo marca uno: falta f2. build_decision rechaza sin tocar la cola.
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, review_key), user_id="u1", actor="Ana"
    )

    assert update is not None and update.card is not None
    assert "f2" in update.notice, "el aviso dice que hallazgo falta"
    assert harness.queue.dequeue() is None
    assert harness.marks.final(review_key) is None


async def test_una_segunda_decision_no_encola_otra_reanudacion() -> None:
    harness, review_key = _harness()
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )
    await harness.ingress.handle(_data(SUBMIT_ACTION, review_key), user_id="u1", actor="Ana")

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, review_key), user_id="u2", actor="Luis"
    )

    assert update is not None and "ya la tomo Ana" in update.notice
    assert harness.queue.dequeue() is not None
    assert harness.queue.dequeue() is None, "solo se encola una reanudacion"


async def test_una_accion_sobre_una_pausa_vencida_no_publica_tarjeta() -> None:
    """La pausa ya no esta en el inbox: la tarjeta quedo obsoleta."""
    harness = _Harness(paused=InMemoryPausedStore())

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, "r:1:abc"), user_id="u1", actor="Ana"
    )

    assert update is not None and update.card is None
    assert "ya no esta pendiente" in update.notice
    assert harness.queue.dequeue() is None


async def test_una_accion_con_un_review_key_vacio_se_ignora() -> None:
    harness, _ = _harness()

    assert await harness.ingress.handle({"action": DECISION_ACTION}, user_id="u", actor="A") is None


async def test_una_accion_desconocida_se_ignora() -> None:
    """El `data.action` es un discriminante: una accion nueva no rompe el ingress."""
    harness, review_key = _harness()

    update = await harness.ingress.handle(_data("teams.ask", review_key), user_id="u1", actor="Ana")

    assert update is None


async def test_una_marca_con_outcome_invalido_no_toca_el_estado() -> None:
    harness, review_key = _harness()

    update = await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="quizas"),
        user_id="u1",
        actor="Ana",
    )

    assert update is not None and "no es valida" in update.notice
    assert harness.marks.marks(review_key, user_id="u1") == {}


async def test_expired_no_es_una_decision_humana() -> None:
    """`expired` lo emite el barrido del TTL, no una persona."""
    harness, review_key = _harness()

    update = await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="expired"),
        user_id="u1",
        actor="Ana",
    )

    assert update is not None and update.card is None
    assert harness.marks.marks(review_key, user_id="u1") == {}


async def test_un_fallo_de_redis_al_marcar_avisa_en_vez_de_romper() -> None:
    """La accion no puede responderle un `500` a Teams: solo un aviso de reintento."""
    harness, review_key = _harness()
    harness.marks.mark = _raise  # type: ignore[method-assign]

    update = await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )

    assert update is not None and update.card is None
    assert "Vuelve a intentarlo" in update.notice


async def test_un_fallo_de_la_cola_al_enviar_avisa_y_no_sella() -> None:
    harness, review_key = _harness()
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )
    harness.queue.enqueue = _raise  # type: ignore[method-assign]

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, review_key), user_id="u1", actor="Ana"
    )

    assert update is not None and "Vuelve a intentarlo" in update.notice
    assert harness.marks.final(review_key) is None, "sin encolar, no se sella la decision"


async def test_si_el_canal_falla_la_decision_ya_esta_encolada() -> None:
    """Repintar la tarjeta es cosmetico: la verdad ya viaja por la cola."""
    harness, review_key = _harness()
    harness.channel.fail = True
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="u1",
        actor="Ana",
    )

    update = await harness.ingress.handle(
        _data(SUBMIT_ACTION, review_key), user_id="u1", actor="Ana"
    )

    assert update is not None and update.notice == "Decision enviada"
    assert harness.queue.dequeue() is not None


def _raise(*args: Any, **kwargs: Any) -> Any:
    raise ConnectionError("Redis caido")


# --- Wiring del SDK -----------------------------------------------------------


class _FakeContext:
    """Contexto del SDK minimo: actividad, `send` y `next` (cadena de handlers)."""

    def __init__(self, activity: Any) -> None:
        self.activity = activity
        self.sent: list[str] = []

    async def send(self, message: Any) -> None:
        self.sent.append(str(message))

    async def next(self) -> None:
        return None


class _FakeApp:
    """Registra los handlers que el wiring da de alta en la `App` del SDK."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def _decorator(self, name: str) -> Any:
        def register(func: Any) -> Any:
            self.handlers[name] = func
            return func

        return register

    def on_activity(self, func: Any) -> Any:
        return self._decorator("activity")(func)

    def on_card_action_execute(self, func: Any) -> Any:
        return self._decorator("card")(func)

    def on_message(self, func: Any) -> Any:
        return self._decorator("message")(func)


class _Activity:
    """Actividad minima con la forma que lee el wiring."""

    def __init__(
        self,
        *,
        value: Any = None,
        aad_object_id: str = "aad-1",
        name: str = "Ana",
        conversation_id: str = "19:canal@thread.tacv2",
    ) -> None:
        self.value = value
        self.service_url = "https://smba"
        self.conversation = type("C", (), {"id": conversation_id})()
        self.from_ = type("F", (), {"aad_object_id": aad_object_id, "id": "u1", "name": name})()


def _wired(harness: _Harness) -> _FakeApp:
    from agent_review.teams_bot.sdk_handlers import register_handlers

    app = _FakeApp()
    register_handlers(app, ingress=harness.ingress, store=harness.store)
    return app


def test_el_wiring_registra_los_tres_handlers() -> None:
    harness, _ = _harness()
    app = _wired(harness)

    assert set(app.handlers) == {"activity", "card", "message"}


async def test_cualquier_actividad_captura_el_canal() -> None:
    """C14: sin canal capturado no hay publicacion proactiva."""
    harness, _ = _harness()
    harness.store = InMemoryChannelStore()
    app = _wired(harness)

    await app.handlers["activity"](_FakeContext(_Activity()))

    context = harness.store.context()
    assert context is not None
    assert context.conversation_id == "19:canal@thread.tacv2"
    assert context.service_url == "https://smba"


async def test_si_redis_falla_al_capturar_el_canal_el_handler_no_rompe() -> None:
    harness, _ = _harness()
    harness.store.remember_context = _raise  # type: ignore[method-assign]
    app = _wired(harness)

    await app.handlers["activity"](_FakeContext(_Activity()))


async def test_el_handler_de_tarjeta_marca_y_devuelve_la_tarjeta() -> None:
    harness, review_key = _harness()
    app = _wired(harness)
    value = type(
        "V",
        (),
        {
            "action": type(
                "A",
                (),
                {"data": _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved")},
            )()
        },
    )()

    response = await app.handlers["card"](_FakeContext(_Activity(value=value)))

    assert response.value.type == "AdaptiveCard", "el invoke responde con la tarjeta"
    assert harness.marks.marks(review_key, user_id="aad-1") == {"f1": DecisionOutcome.APPROVED}


async def test_el_handler_de_tarjeta_usa_el_id_de_entra_del_que_pulsa() -> None:
    """C5: `actor` es el nombre visible; el estado se indexa por el id de Entra."""
    harness, review_key = _harness()
    app = _wired(harness)
    await harness.ingress.handle(
        _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved"),
        user_id="aad-1",
        actor="Ana",
    )
    value = type("V", (), {"action": type("A", (), {"data": _data(SUBMIT_ACTION, review_key)})()})()

    await app.handlers["card"](_FakeContext(_Activity(value=value, name="Ana")))

    enqueued = harness.queue.dequeue()
    assert enqueued is not None and enqueued.decision is not None
    assert enqueued.decision.actor == "Ana"


async def test_sin_tarjeta_que_repintar_el_invoke_responde_un_mensaje() -> None:
    """El protocolo admite tarjeta o mensaje: una pausa vencida no tiene tarjeta."""
    harness = _Harness(paused=InMemoryPausedStore())
    app = _wired(harness)
    value = type("V", (), {"action": type("A", (), {"data": _data(SUBMIT_ACTION, "r:1:abc")})()})()

    response = await app.handlers["card"](_FakeContext(_Activity(value=value)))

    assert response.type == "application/vnd.microsoft.activity.message"
    assert "ya no esta pendiente" in response.value


async def test_el_fallback_submit_llega_por_on_message() -> None:
    """T0: los clientes viejos mandan la accion como `value` de un mensaje."""
    harness, review_key = _harness()
    app = _wired(harness)
    ctx = _FakeContext(
        _Activity(value=_data(DECISION_ACTION, review_key, finding_id="f1", outcome="noted"))
    )

    await app.handlers["message"](ctx)

    assert harness.marks.marks(review_key, user_id="aad-1") == {"f1": DecisionOutcome.NOTED}


async def test_un_mensaje_normal_no_se_interpreta_como_accion() -> None:
    harness, review_key = _harness()
    app = _wired(harness)
    ctx = _FakeContext(_Activity(value=None))

    await app.handlers["message"](ctx)

    assert ctx.sent == [], "un mensaje de texto no se responde"
    assert harness.marks.marks(review_key, user_id="aad-1") == {}


# --- Pipeline real del SDK sobre FastAPI --------------------------------------


def _invoke_activity(data: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "id": "act-1",
        "channelId": "msteams",
        "serviceUrl": "https://smba",
        "from": {"id": "u1", "aadObjectId": "aad-1", "name": "Ana"},
        "recipient": {"id": "bot-1", "name": "agent-review"},
        "conversation": {"id": "19:canal@thread.tacv2", "conversationType": "channel"},
        "value": {"action": {"type": "Action.Execute", "verb": data["action"], "data": data}},
    }


def _real_app(harness: _Harness, *, auth: bool) -> Any:
    from microsoft_teams.apps import App

    from agent_review.teams_bot.sdk_handlers import register_handlers

    app = App(dangerously_allow_unauthenticated_requests=not auth)
    register_handlers(app, ingress=harness.ingress, store=harness.store)
    return app


async def test_el_pipeline_real_del_sdk_encola_la_decision_de_la_tarjeta() -> None:
    """Prueba de integracion: `POST /api/messages` -> ingress -> cola, sin Teams."""
    harness, review_key = _harness()
    harness.store = InMemoryChannelStore()
    app = _real_app(harness, auth=False)
    await app.initialize()
    transport = httpx.ASGITransport(app=app.server.adapter.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
        await client.post(
            "/api/messages",
            json=_invoke_activity(
                _data(DECISION_ACTION, review_key, finding_id="f1", outcome="approved")
            ),
        )
        response = await client.post(
            "/api/messages", json=_invoke_activity(_data(SUBMIT_ACTION, review_key))
        )

    assert response.status_code == 200
    enqueued = harness.queue.dequeue()
    assert enqueued is not None and enqueued.decision is not None
    assert enqueued.decision.actor == "Ana"
    assert harness.store.context() is not None, "el invoke tambien captura el canal (C14)"
    body = response.json()
    assert body["type"] == "application/vnd.microsoft.card.adaptive"
    assert body["value"]["body"], "la tarjeta repintada viaja en la respuesta"


@pytest.mark.parametrize("header", ["", "Basic abc", "Bearer"])
async def test_sin_credenciales_del_bot_el_endpoint_rechaza(header: str) -> None:
    """Fail-closed: sin identidad del bot no hay token validator, y se rechaza."""
    harness, _ = _harness()
    app = _real_app(harness, auth=True)
    await app.initialize()
    transport = httpx.ASGITransport(app=app.server.adapter.app)
    headers = {"Authorization": header} if header else {}

    async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
        response = await client.post(
            "/api/messages",
            json=_invoke_activity(_data(SUBMIT_ACTION, "r:1:abc")),
            headers=headers,
        )

    assert response.status_code == 401
    assert harness.queue.dequeue() is None, "una peticion sin validar no toca la cola"
