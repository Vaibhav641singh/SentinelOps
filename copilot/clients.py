"""Provider clients and the retry/failover policy wrapped around them."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google import genai
from google.genai.errors import ClientError, ServerError
from qdrant_client import AsyncQdrantClient, QdrantClient

from .config import (
    FALLBACK_MODELS,
    GEMINI_API_KEY,
    GEMINI_KEY_PRESENT,
    QDRANT_API_KEY,
    QDRANT_PATH,
    QDRANT_URL,
)
from .errors import ConfigurationError, UpstreamRejected, UpstreamUnavailable

log = logging.getLogger(__name__)

T = TypeVar("T")

RATE_LIMITED = 429


def gemini_client() -> genai.Client:
    if not GEMINI_KEY_PRESENT:
        raise ConfigurationError(
            "GEMINI_API_KEY is missing or still the placeholder. "
            "Put a real key in .env — get one at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=GEMINI_API_KEY)


def qdrant_client() -> QdrantClient:
    """Server/Cloud when QDRANT_URL is set, otherwise embedded on local disk.

    Embedded mode takes an exclusive directory lock, so only one process may hold
    it at a time: no `uvicorn --reload`, no `--workers N`.
    """
    if QDRANT_URL:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return QdrantClient(path=QDRANT_PATH)


def async_qdrant_client() -> AsyncQdrantClient:
    if QDRANT_URL:
        return AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return AsyncQdrantClient(path=QDRANT_PATH)


def models_to_try(preferred: str) -> list[str]:
    return [preferred] + [m for m in FALLBACK_MODELS if m != preferred]


def backoff_for(exc: Exception, attempt: int) -> float:
    """Rate limits deserve a longer pause than a capacity blip."""
    return 2.0 * (2**attempt) if isinstance(exc, ClientError) else 0.6 * (2**attempt)


def is_retryable(exc: Exception) -> bool:
    """503 is transient capacity; 429 is a rate limit — both worth retrying.

    Every other ClientError is a bad request, where retrying just wastes time.
    """
    if isinstance(exc, ServerError):
        return True
    return isinstance(exc, ClientError) and getattr(exc, "code", None) == RATE_LIMITED


async def with_failover(
    preferred: str,
    call: Callable[[str], Awaitable[T]],
    attempts: int = 2,
) -> T:
    """Retry `call` on transient upstream failures, then try the next model."""
    last: Exception | None = None
    for model in models_to_try(preferred):
        for attempt in range(attempts):
            try:
                return await call(model)
            except (ServerError, ClientError) as exc:
                if not is_retryable(exc):
                    raise UpstreamRejected(f"Gemini rejected the request: {exc}") from exc
                last = exc
                log.warning("model=%s attempt=%d failed: %s", model, attempt + 1, exc)
                await asyncio.sleep(backoff_for(exc, attempt))

    raise UpstreamUnavailable(
        "Gemini is unavailable on every configured model (overload or rate limit) — "
        f"this is transient, try again shortly. Last error: {last}"
    )
