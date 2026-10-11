from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from reviews.adapters.tooling_source import FileToolingOutputSource, validate_tooling_template
from reviews.domain.models import ReviewRequest
from reviews.domain.pending import InMemoryPendingStore, PendingStatus
from reviews.idempotency import IdempotencyStore, InMemoryIdempotencyStore, RedisIdempotencyStore
from reviews.pending_store import RedisPendingStore
from reviews.profile import expected_tools
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue, ReviewQueue
from reviews.settings import Settings, get_settings, require_production_secrets

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


app = FastAPI(title="reviews", version="0.1.0", lifespan=lifespan)


def _settings() -> Settings:
    return get_settings()


_fallback_store: InMemoryIdempotencyStore | None = None
_fallback_queue: InMemoryReviewQueue | None = None
_fallback_pending: InMemoryPendingStore | None = None


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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _verify_signature(secret: str, raw_body: bytes, signature: str | None) -> None:
    if not secret or secret == "change-me":
        return
    if not signature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta la firma del webhook")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma del webhook invalida")


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

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payload no es un objeto JSON")

    tool = str(body.get("tool", ""))
    payload = body.get("payload")
    review_request = ReviewRequest(
        repo=str(body.get("repo", "")),
        pr_id=int(body.get("pr_id", 0) or 0),
        head_sha=str(body.get("head_sha", "")),
        pipeline_id=str(body.get("pipeline_id", "")),
    )
    if not (review_request.repo and review_request.pr_id and review_request.head_sha and tool):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Faltan repo, pr_id, head_sha o tool")
    if not isinstance(payload, str) or not payload.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El payload del artefacto esta vacio")

    store = build_store(settings)
    if not store.claim(review_request.ingest_key(tool)):
        return JSONResponse({"status": "duplicado", "key": review_request.ingest_key(tool)})

    _persist_artifact(settings, review_request, tool, payload)

    pending = build_pending(settings)
    pending_status = pending.observe(review_request, tool, payload)

    if pending_status is PendingStatus.READY:
        # El artefacto esta completo: ahora si se dispara la review. La clave de
        # review es distinta de la de ingesta para que el evento de CI y la
        # ejecucion no se confundan como duplicados.
        if store.claim(review_request.review_key):
            build_queue(settings).enqueue(review_request)
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
    """Aplica los TTL de la ingesta y devuelve las reviews degradadas."""
    settings = _settings()
    pending = build_pending(settings)
    degraded = pending.sweep()
    return {"degraded": [review.key for review in degraded]}


@app.get("/profile/{repo_name}")
def get_profile(repo_name: str) -> dict[str, object]:
    """Devuelve el perfil de repo cargado, para inspeccion y depuracion."""
    del repo_name
    settings = _settings()
    path = Path(settings.repo_profile_config)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el perfil {path}")
    return {"path": str(path), "exists": True}
