from __future__ import annotations

import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from reviews.domain.models import ReviewRequest
from reviews.idempotency import InMemoryIdempotencyStore, RedisIdempotencyStore
from reviews.queue import InMemoryReviewQueue, RedisReviewQueue
from reviews.settings import Settings, get_settings

logger = logging.getLogger(__name__)

app = FastAPI(title="reviews", version="0.1.0")


def _settings() -> Settings:
    return get_settings()


_fallback_store: InMemoryIdempotencyStore | None = None
_fallback_queue: InMemoryReviewQueue | None = None


def build_store(settings: Settings):
    """Redis si esta disponible; si no, un almacen en memoria compartido."""
    global _fallback_store
    store = RedisIdempotencyStore(url=settings.redis_url)
    if store.available():
        return store
    if _fallback_store is None:
        logger.warning("Redis no disponible; uso almacen de memoria para idempotencia")
        _fallback_store = InMemoryIdempotencyStore()
    return _fallback_store


def build_queue(settings: Settings):
    """Redis si esta disponible; si no, cola en memoria compartida (tests/local)."""
    global _fallback_queue
    queue = RedisReviewQueue(url=settings.redis_url)
    if queue.available():
        return queue
    if _fallback_queue is None:
        logger.warning("Redis no disponible; uso cola en memoria")
        _fallback_queue = InMemoryReviewQueue()
    return _fallback_queue


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


@app.post("/webhook/bitbucket")
async def bitbucket_webhook(
    request: Request,
    x_hub_signature: str | None = Header(default=None),
) -> JSONResponse:
    settings = _settings()
    raw = await request.body()
    _verify_signature(settings.bitbucket_webhook_secret, raw, x_hub_signature)

    payload = await request.json()
    change = (payload.get("pullrequest") or {}) if isinstance(payload, dict) else {}
    if not change and isinstance(payload, dict):
        change = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

    repo = str(change.get("destination", {}).get("repository", {}).get("full_name", "")) or str(
        settings.bitbucket_repo
    )
    pr_id = int(change.get("id", 0) or 0)
    head_sha = str(change.get("source", {}).get("commit", {}).get("hash", "")) or ""

    if not pr_id or not head_sha:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Faltan pr_id o head_sha")

    review_request = ReviewRequest(repo=repo, pr_id=pr_id, head_sha=head_sha)
    store = build_store(settings)
    if not store.claim(review_request.idempotency_key):
        return JSONResponse({"status": "duplicado", "key": review_request.idempotency_key})

    queue = build_queue(settings)
    queue.enqueue(review_request)
    logger.info("Review encolada: %s", review_request.idempotency_key)
    return JSONResponse(
        {"status": "aceptado", "key": review_request.idempotency_key},
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
