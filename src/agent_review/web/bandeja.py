"""Bandeja de decisiones del panel (ADR-0030).

Lista las pausas humanas abiertas —las mismas que Teams pintaria como tarjeta— y
responde con el `DecisionSubmitter`: el sello de la primera respuesta es unico.
Si Teams esta configurado, ganar aqui encola un `withdraw` para que el bot borre
la tarjeta de alla. Si no lo esta, la bandeja es la unica superficie.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from agent_review.chat_outbox import RedisChatOutbox
from agent_review.domain.models import FindingDecision
from agent_review.domain.paused import PauseKind
from agent_review.paused_store import PausedStore, RedisPausedStore
from agent_review.queue import RedisReviewQueue, ReviewQueue
from agent_review.settings import Settings, get_settings
from agent_review.teams_bot.decisions import DecisionSubmitter, SubmitOutcome, SubmitStatus
from agent_review.teams_bot.state import CardStore, RedisCardStore
from agent_review.teams_bot.withdrawal import OutboxPauseWithdrawal
from agent_review.web.auth import Session, require_command_session, require_session

router = APIRouter()


class BandejaPending(BaseModel):
    """Un hallazgo de la tarjeta, con lo que hace falta para responder."""

    finding_id: str
    title: str
    severity: str
    file: str
    line: int
    why_it_matters: str
    proposed_action: str
    reason: str
    code_url: str | None = None


class BandejaItem(BaseModel):
    """Una pregunta abierta. `pause_id` distingue dos pausas del mismo hilo."""

    review_key: str
    pause_id: str
    repo: str
    pr_id: int
    pause_kind: str | None = None
    created_at: datetime
    pending: list[BandejaPending] = Field(default_factory=list)


class BandejaView(BaseModel):
    """Lo que el panel sondea: las preguntas abiertas y si Teams tambien las ve."""

    teams_enabled: bool
    items: list[BandejaItem]


class BandejaService:
    """Lectura de las pausas humanas sin sello, y su respuesta."""

    def __init__(
        self,
        *,
        paused: PausedStore,
        cards: CardStore,
        submitter: DecisionSubmitter,
        queue: ReviewQueue,
        teams_enabled: bool,
    ) -> None:
        self.paused = paused
        self.cards = cards
        self.submitter = submitter
        self.queue = queue
        self.teams_enabled = teams_enabled

    def view(self) -> BandejaView:
        """Pausas humanas aun sin responder, de la mas antigua a la mas nueva."""
        items: list[BandejaItem] = []
        for review in self.paused.all():
            if review.kind is not PauseKind.HUMAN:
                continue
            if self.cards.final(review.review_key, pause=review.pause_id) is not None:
                continue
            approval = review.human_request
            items.append(
                BandejaItem(
                    review_key=review.review_key,
                    pause_id=review.pause_id,
                    repo=approval.repo,
                    pr_id=approval.pr_id,
                    pause_kind=approval.pause_kind,
                    created_at=review.created_at,
                    pending=[
                        BandejaPending(
                            finding_id=item.finding_id,
                            title=item.title,
                            severity=item.severity.value,
                            file=item.file,
                            line=item.line,
                            why_it_matters=item.why_it_matters,
                            proposed_action=item.proposed_action.value,
                            reason=item.reason,
                            code_url=item.code_url,
                        )
                        for item in approval.pending
                    ],
                )
            )
        items.sort(key=lambda item: item.created_at)
        return BandejaView(teams_enabled=self.teams_enabled, items=items)

    def submit(
        self,
        review_key: str,
        *,
        actor: str,
        items: list[FindingDecision] | None = None,
        batch: str | None = None,
    ) -> SubmitOutcome:
        """Encola la respuesta. `batch` cubre una pausa de publicacion de un golpe."""
        if batch is not None:
            return self.submitter.submit_batch(
                review_key, actor=actor, batch=batch, origin="bandeja"
            )
        return self.submitter.submit_items(
            review_key, actor=actor, items=items or [], origin="bandeja"
        )


def build_bandeja(settings: Settings) -> BandejaService:
    """Bandeja sobre Redis. Con Teams, la respuesta tambien retira la tarjeta."""
    paused = RedisPausedStore(url=settings.redis_url, ttl_s=settings.decision_ttl_s)
    cards = RedisCardStore(url=settings.redis_url, ttl_s=int(settings.decision_ttl_s))
    queue = RedisReviewQueue(url=settings.redis_url)
    withdrawal = None
    if settings.teams_enabled:
        withdrawal = OutboxPauseWithdrawal(
            RedisChatOutbox(url=settings.redis_url, key=settings.teams_notices_key)
        )
    submitter = DecisionSubmitter(paused=paused, queue=queue, cards=cards, withdrawal=withdrawal)
    return BandejaService(
        paused=paused,
        cards=cards,
        submitter=submitter,
        queue=queue,
        teams_enabled=settings.teams_enabled,
    )


def get_bandeja(request: Request) -> BandejaService:
    """Servicio del proceso. Los tests inyectan uno en `app.state.bandeja`."""
    service: BandejaService | None = getattr(request.app.state, "bandeja", None)
    if service is None:
        settings = get_settings()
        if not settings.redis_url.strip():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "La bandeja necesita Redis, que no esta disponible",
            )
        service = build_bandeja(settings)
        request.app.state.bandeja = service
    return service


@router.get("/api/bandeja")
def list_bandeja(
    session: Annotated[Session, Depends(require_session)],
    bandeja: Annotated[BandejaService, Depends(get_bandeja)],
) -> BandejaView:
    """Preguntas abiertas. Cualquier sesion las ve; responder exige operador."""
    del session
    return bandeja.view()


@router.post("/api/bandeja/{review_key:path}/decision")
async def decide_bandeja(
    review_key: str,
    request: Request,
    session: Annotated[Session, Depends(require_command_session)],
    bandeja: Annotated[BandejaService, Depends(get_bandeja)],
) -> dict[str, object]:
    """Responde una pregunta. Vale la primera respuesta, tambien si llego por Teams."""
    items, batch = await _decision_body(request)
    try:
        outcome = bandeja.submit(review_key, actor=session.user, items=items, batch=batch)
    except Exception as exc:  # noqa: BLE001 - un Redis caido no puede parecer un 200
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return _effect(outcome, review_key, session.user)


async def _decision_body(
    request: Request,
) -> tuple[list[FindingDecision] | None, str | None]:
    """`items` o `batch`, no los dos. Un cuerpo que no encaja es `400`."""
    try:
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El cuerpo no es JSON")
    has_items = "items" in raw
    has_batch = "batch" in raw
    if has_items == has_batch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hace falta `items` o `batch`")
    if has_batch:
        batch = raw.get("batch")
        if not isinstance(batch, str) or not batch.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "El lote no es valido")
        return None, batch
    if not isinstance(raw.get("items"), list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta `items`")
    try:
        return [
            FindingDecision.model_validate(item) if isinstance(item, dict) else item
            for item in raw["items"]
        ], None
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Item de decision invalido: {exc}"
        ) from exc


def _effect(outcome: SubmitOutcome, review_key: str, actor: str) -> dict[str, object]:
    if outcome.status is SubmitStatus.ENQUEUED:
        return {"review_key": review_key, "effect": "enqueued", "actor": actor}
    if outcome.status is SubmitStatus.ALREADY_DECIDED:
        who = outcome.actor or "otra persona"
        raise HTTPException(status.HTTP_409_CONFLICT, f"La decision ya la tomo {who}")
    raise HTTPException(status.HTTP_409_CONFLICT, outcome.reason or "La review ya no esta pausada")
