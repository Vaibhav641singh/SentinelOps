"""HTTP surface. The only module that knows about status codes."""

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .clients import async_qdrant_client
from .config import AUTH_ENABLED, COLLECTION, GEMINI_KEY_PRESENT, QDRANT_URL, STATIC_DIR
from .errors import ConfigurationError, UpstreamRejected, UpstreamUnavailable
from .models import DiagnoseRequest
from .pipeline import diagnose_events
from .security import require_api_key

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s | %(message)s"
)

app = FastAPI(
    title="DevOps AI Copilot",
    version="1.0.0",
    description=(
        "Paste a server error log, get suggested recovery commands. "
        "Suggestions only — a human reviews and runs them."
    ),
)

_STATUS = {
    UpstreamUnavailable: 503,
    UpstreamRejected: 502,
    ConfigurationError: 500,
}


@app.exception_handler(UpstreamUnavailable)
@app.exception_handler(UpstreamRejected)
@app.exception_handler(ConfigurationError)
async def handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
    """Map domain errors to status codes here, so no lower layer imports HTTP."""
    return JSONResponse(status_code=_STATUS.get(type(exc), 500), content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    qdrant = async_qdrant_client()
    try:
        exists = await qdrant.collection_exists(COLLECTION)
        points = (await qdrant.count(COLLECTION)).count if exists else 0
    finally:
        await qdrant.close()

    return {
        "status": "ok",
        "auth": "enabled" if AUTH_ENABLED else "DISABLED",
        "gemini_key_loaded": GEMINI_KEY_PRESENT,
        "qdrant_mode": "server" if QDRANT_URL else "embedded",
        "collection": COLLECTION,
        "points": points,
    }


@app.post("/diagnose", dependencies=[Depends(require_api_key)])
async def diagnose(request: DiagnoseRequest) -> StreamingResponse:
    return StreamingResponse(
        diagnose_events(request.error_log),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
