from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from agent_review.adapters.tooling_source import FileToolingOutputSource, validate_tooling_template
from agent_review.decision import DecisionError, build_decision, require_decision_item
from agent_review.domain.models import ReviewRequest
from agent_review.domain.paused import PausedReview, PauseKind
from agent_review.domain.pending import InMemoryPendingStore, PendingStatus
from agent_review.idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from agent_review.paused_store import InMemoryPausedStore, RedisPausedStore
from agent_review.pending_store import RedisPendingStore
from agent_review.profile import expected_tools
from agent_review.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from agent_review.settings import Settings, get_settings, is_missing, require_production_secrets

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # En produccion, arrancar con un secreto de ejemplo seria un agujero: la firma
    # del webhook de tooling se omitiria (D4). La plantilla del artefacto tambien
    # se valida aqui (D2): un cambio en el CI que la rompa falla al arrancar.
    settings = get_settings()
    require_production_secrets(settings)
    validate_tooling_template(
        template=settings.tooling_output_template, root=settings.tooling_output_root
    )
    yield


app = FastAPI(title="Agent Review LangGraph", version="0.1.0", lifespan=lifespan)


def _settings() -> Settings:
    return get_settings()


_fallback_store: InMemoryIdempotencyStore | None = None
_fallback_queue: InMemoryReviewQueue | None = None
_fallback_pending: InMemoryPendingStore | None = None
_fallback_paused: InMemoryPausedStore | None = None


def build_store(settings: Settings) -> IdempotencyStore:
    """Redis si esta disponible; si no, un almacen en memoria compartido."""
    global _fallback_store
    store = RedisIdempotencyStore(url=settings.redis_url)
    if store.available():
        return store
    if _fallback_store is None:
        logger.warning("Redis no disponible; uso almacen de memoria para idempotencia")
        _fallback_store = InMemoryIdempotencyStore()
    return _fallback_store


def build_queue(settings: Settings) -> ReviewQueue:
    """Redis si esta disponible; si no, cola en memoria compartida (tests/local)."""
    global _fallback_queue
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    if _fallback_queue is None:
        logger.warning("Redis no disponible; uso cola en memoria")
        _fallback_queue = InMemoryReviewQueue()
    return _fallback_queue


def build_pending(settings: Settings) -> RedisPendingStore | InMemoryPendingStore:
    """Registro de artefactos pendientes por review."""
    global _fallback_pending
    tools = expected_tools(settings)
    store = RedisPendingStore(
        url=settings.redis_url,
        expected_tools=tools,
        ttl_ready_s=settings.ingest_ttl_ready_s,
        ttl_pending_s=settings.ingest_ttl_pending_s,
    )
    if store.available():
        return store
    if _fallback_pending is None:
        logger.warning("Redis no disponible; uso registro pending en memoria")
        _fallback_pending = InMemoryPendingStore(expected_tools=tools)
    return _fallback_pending


def build_paused(settings: Settings) -> RedisPausedStore | InMemoryPausedStore:
    """Inbox de reviews pausadas: indice para validar y descubrir decisiones."""
    global _fallback_paused
    store = RedisPausedStore(url=settings.redis_url)
    if store.available():
        return store
    if _fallback_paused is None:
        logger.warning("Redis no disponible; uso inbox de pausas en memoria")
        _fallback_paused = InMemoryPausedStore()
    return _fallback_paused


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _verify_signature(secret: str, raw_body: bytes, signature: str | None) -> None:
    if is_missing(secret):
        return
    if not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta la firma del webhook")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma del webhook invalida")


def _verify_decision_token(settings: Settings, authorization: str | None) -> None:
    """Bearer del canal de decision; relajado con el valor de ejemplo en dev."""
    token = settings.reviews_decision_token.strip()
    if is_missing(token):
        return
    expected = f"Bearer {token}"
    if not authorization or not hmac.compare_digest(expected, authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token de decision invalido")


def _expected_repo(settings: Settings) -> str:
    """Identidad `owner/repo` del repo que revisa este despliegue, o "" si no hay."""
    return settings.configured_repo()


def _verify_repo(settings: Settings, repo: str) -> None:
    """Rechaza artefactos de un repo distinto del que revisa este despliegue.

    El adaptador de VCS ignora el `repo` del job y siempre usa el repo
    configurado (`BITBUCKET_WORKSPACE/BITBUCKET_REPO` o `GITHUB_OWNER/GITHUB_REPO`):
    sin este filtro, el CI de otro repo (o un error de configuracion) haria que el
    agente revisara y comentara el PR con el mismo numero del repo configurado.
    Sin repo configurado no hay con que comparar (el worker tampoco podria
    publicar) y no se filtra.
    """
    expected = _expected_repo(settings)
    if not expected:
        return
    if repo.strip().casefold() != expected.casefold():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Repo {repo!r} no admitido: este despliegue solo revisa {expected}",
        )


def _release_quietly(store: IdempotencyStore, *keys: str) -> None:
    """Libera cada clave de la compensacion sin enmascarar el error original.

    Corre dentro de un `except` que va a relanzar: si una liberacion lanzara
    (Redis caido), taparia la excepcion original y dejaria sin liberar las
    claves siguientes.
    """
    for key in keys:
        try:
            store.release(key)
        except Exception:
            logger.exception("No se pudo liberar la clave %s; queda reclamada", key)


def _persist_artifact(settings: Settings, request: ReviewRequest, tool: str, payload: str) -> Path:
    """Deposita el artefacto en la ruta que resuelve el `ToolingOutputSource`."""
    source = FileToolingOutputSource(
        template=settings.tooling_output_template, root=settings.tooling_output_root
    )
    path = source.path_for(tool, repo=request.repo, pr_id=request.pr_id, head_sha=request.head_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


@app.post("/webhook/tooling")
async def tooling_webhook(
    request: Request,
    x_reviews_signature: str | None = Header(default=None),
) -> JSONResponse:
    """El CI del repo revisado deposita aqui la salida de una herramienta estatica.

    Sustituye al disparo por PR: la review solo se encola cuando el artefacto que
    necesita ya esta dentro, de modo que no compite contra el propio CI.
    """
    settings = _settings()
    raw = await request.body()
    max_bytes = settings.max_tooling_payload_bytes
    if len(raw) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"El payload supera el limite de {max_bytes} bytes",
        )
    _verify_signature(settings.tooling_webhook_secret, raw, x_reviews_signature)

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no es un objeto JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no es un objeto JSON")

    tool = str(body.get("tool", ""))
    payload = body.get("payload")
    try:
        pr_id = int(body.get("pr_id", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "pr_id no es un entero") from exc
    review_request = ReviewRequest(
        repo=str(body.get("repo", "")),
        pr_id=pr_id,
        head_sha=str(body.get("head_sha", "")),
        base_sha=str(body.get("base_sha", "")),
        pipeline_id=str(body.get("pipeline_id", "")),
    )
    if not (review_request.repo and review_request.pr_id and review_request.head_sha and tool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Faltan repo, pr_id, head_sha o tool")
    allowed = expected_tools(settings)
    if allowed and tool not in allowed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tool no esperada: {tool}")
    if not isinstance(payload, str) or not payload.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El payload del artefacto esta vacio")
    _verify_repo(settings, review_request.repo)

    store = build_store(settings)
    ingest_key = review_request.ingest_key(tool)
    if not store.claim(ingest_key):
        return JSONResponse({"status": "duplicado", "key": ingest_key})

    # Cada `claim` se deshace si falla el paso que protege: si no, la clave queda
    # reclamada 7 dias y el reintento del CI recibe `duplicado` con el artefacto o
    # la review perdidos en silencio.
    try:
        _persist_artifact(settings, review_request, tool, payload)
        pending = build_pending(settings)
        pending_status = pending.observe(review_request, tool, payload)
    except ValueError as exc:
        _release_quietly(store, ingest_key)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        _release_quietly(store, ingest_key)
        raise

    if pending_status is PendingStatus.READY:
        # El artefacto esta completo: ahora si se dispara la review. La clave de
        # review es distinta de la de ingesta para que el evento de CI y la
        # ejecucion no se confundan como duplicados.
        if store.claim(review_request.review_key):
            try:
                build_queue(settings).enqueue(review_request)
            except Exception:
                # Se liberan las dos: el reintento del CI tiene que poder volver a
                # observar el artefacto y disparar la review.
                _release_quietly(store, review_request.review_key, ingest_key)
                raise
            logger.info("Review encolada: %s", review_request.review_key)
            return JSONResponse(
                {"status": "aceptado", "key": review_request.review_key},
                status_code=status.HTTP_202_ACCEPTED,
            )
        return JSONResponse({"status": "duplicado", "key": review_request.review_key})

    record = pending.get(review_request.idempotency_key)
    missing = ", ".join(record.missing) if record else "?"
    logger.info(
        "Artefacto %s recibido para %s; pendiente de %s",
        tool,
        review_request.idempotency_key,
        missing,
    )
    return JSONResponse(
        {"status": "pendiente", "key": review_request.idempotency_key},
        status_code=status.HTTP_202_ACCEPTED,
    )


@app.get("/pending")
def list_pending() -> dict[str, object]:
    """Lista la ingesta vencida o ya degradada, sin aplicar el TTL.

    Degradar y encolar es del barrido del worker. Si este endpoint lo hiciera,
    `is_expired` dejaria de ver la review y el worker no la encolaria.
    """
    settings = _settings()
    pending = build_pending(settings)
    due = pending.due()
    return {
        "expired": [review.key for review in due if review.status is not PendingStatus.DEGRADED],
        "degraded": [review.key for review in due if review.status is PendingStatus.DEGRADED],
    }


@app.get("/reviews/paused")
def list_paused() -> dict[str, object]:
    """Reviews pausadas: humanas (esperan decision) o de fix (esperan verificacion)."""
    settings = _settings()
    paused = build_paused(settings).all()
    return {"paused": [_paused_payload(review) for review in paused]}


def _paused_payload(review: PausedReview) -> dict[str, object]:
    """Forma comun por kind: `human` expone pendientes; `fix`, la wave en curso."""
    payload: dict[str, object] = {
        "review_key": review.review_key,
        "kind": review.kind.value,
        "created_at": review.created_at.isoformat(),
    }
    if review.kind is PauseKind.HUMAN:
        approval = review.human_request
        payload["state"] = "PAUSED"
        payload["pending"] = [item.model_dump(mode="json") for item in approval.pending]
        payload["command_hint"] = approval.command_hint
    else:
        wave = review.fix_request
        payload["state"] = "VERIFYING"
        payload["jobs"] = [
            {"run_key": job.run_key, "finding_id": job.finding_id} for job in wave.jobs
        ]
    return payload


@app.get("/reviews/{review_key:path}")
def get_review_state(review_key: str) -> dict[str, object]:
    """Estado de una review segun el inbox: `PAUSED`, `VERIFYING` o desconocido.

    El API no habla con el checkpointer (spec §4.9): la verdad durable vive en
    Postgres y su indice de descubrimiento es el inbox. Por eso solo se puede
    afirmar `PAUSED`/`VERIFYING` (esta en el inbox) o `UNKNOWN` (no consta pausada;
    puede estar completada o ser ajena). Devuelve 404 si no consta pausada.
    """
    settings = _settings()
    paused = build_paused(settings).get(review_key)
    if paused is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{review_key} no consta como pausada")
    return _paused_payload(paused)


@app.post("/reviews/{review_key:path}/decision")
async def post_decision(
    review_key: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Encola una decision humana sobre una review pausada.

    No toca el grafo: valida contra el inbox y encola un job de reanudacion. El
    worker aplica `Command(resume=...)` sobre el mismo hilo (spec §4.4). Rechaza
    (409) si la review no esta pausada o el `finding_id` no esta entre pendientes.

    El `:path` es necesario porque la `review_key` es `repo:pr:sha[:pipeline]` y el
    repo lleva `/` (`cqm/emotivoo-v2`): sin el, el routing corta en el primer `/`.
    """
    settings = _settings()
    _verify_decision_token(settings, authorization)

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no es un objeto JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no es un objeto JSON")
    actor = str(body.get("actor", ""))
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Falta `items`")

    store = build_paused(settings)
    paused = store.get(review_key)
    if paused is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"La review {review_key} no esta pausada")
    if paused.kind is not PauseKind.HUMAN:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La review espera la verificacion de un fix, no una decision",
        )

    try:
        items = [require_decision_item(item) for item in raw_items]
        decision = build_decision(paused, actor=actor, items=items, reviewed_key=review_key)
    except DecisionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    build_queue(settings).enqueue(paused.request.model_copy(update={"decision": decision}))
    logger.info(
        "Decision encolada para %s: %d hallazgo(s), actor=%s",
        decision.review_key,
        len(decision.items),
        decision.actor,
    )
    return JSONResponse(
        {
            "status": "aceptado",
            "review_key": decision.review_key,
            "decision_id": decision.decision_id,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


@app.get("/profile/{repo_name}")
def get_profile(repo_name: str) -> dict[str, object]:
    """Devuelve el perfil de repo cargado, para inspeccion y depuracion."""
    del repo_name
    settings = _settings()
    path = Path(settings.repo_profile_config)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el perfil {path}")
    return {"path": str(path), "exists": True}
