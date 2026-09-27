"""Shared-key auth for the endpoints that cost money to serve."""

import logging
import secrets

from fastapi import Header, HTTPException

from .config import APP_API_KEY, AUTH_ENABLED

log = logging.getLogger(__name__)

if not AUTH_ENABLED:
    log.warning(
        "APP_API_KEY is not set — /diagnose is UNAUTHENTICATED. Fine on localhost; "
        "set APP_API_KEY before exposing this to a network."
    )


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """Accept the key as either `X-API-Key` or `Authorization: Bearer`.

    Disabled when APP_API_KEY is unset so local development needs no setup. That
    fails open, so it is stated out loud — a startup warning here, and `auth:
    DISABLED` on /healthz — rather than quietly pretending to be protected.
    """
    if not AUTH_ENABLED:
        return

    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    # compare_digest, not ==, so response timing cannot leak the key's prefix.
    if not presented or not secrets.compare_digest(presented, APP_API_KEY):
        raise HTTPException(
            401,
            "Missing or invalid API key. Send it as 'X-API-Key: <key>' or "
            "'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
