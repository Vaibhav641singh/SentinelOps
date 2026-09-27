"""Turning a runbook excerpt — patched or not — into recovery commands."""

import asyncio
import logging
from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError

from .clients import backoff_for, is_retryable, models_to_try
from .config import SYNTH_MODEL
from .errors import UpstreamRejected, UpstreamUnavailable
from .prompts import MERGE_SYSTEM, MERGE_USER, SYNTH_SYSTEM, SYNTH_USER, WEAK_MATCH_NOTE
from .research import Research, format_research

log = logging.getLogger(__name__)


def build_prompt(
    error_log: str,
    chunk: str,
    source: str,
    research: list[Research] | None = None,
    weak: bool = False,
) -> tuple[str, str]:
    """Return (system, user) for whichever of the three paths applies."""
    if research:
        return MERGE_SYSTEM, MERGE_USER.format(
            error_log=error_log,
            source=source,
            chunk=chunk,
            count=len(research),
            research=format_research(research),
        )

    user = SYNTH_USER.format(error_log=error_log, source=source, chunk=chunk)
    if weak:
        user = f"{WEAK_MATCH_NOTE}\n\n{user}"
    return SYNTH_SYSTEM, user


async def synthesize_stream(
    gemini: genai.Client,
    error_log: str,
    chunk: str,
    source: str,
    *,
    research: list[Research] | None = None,
    weak: bool = False,
) -> AsyncIterator[str]:
    """Stream the answer, retrying and failing over on transient failures.

    The first chunk is pulled inside the guarded block because a failure
    surfaces either when opening the stream or on its first read. Once any text
    has been yielded the model is committed — failing over then would duplicate
    output mid-answer.
    """
    system, contents = build_prompt(error_log, chunk, source, research, weak)
    config = types.GenerateContentConfig(system_instruction=system)

    last: Exception | None = None
    for model in models_to_try(SYNTH_MODEL):
        for attempt in range(2):
            try:
                stream = await gemini.aio.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )
                parts = stream.__aiter__()
                first = await anext(parts, None)
            except (ServerError, ClientError) as exc:
                if not is_retryable(exc):
                    raise UpstreamRejected(f"Gemini rejected the request: {exc}") from exc
                last = exc
                log.warning("synthesis model=%s attempt=%d: %s", model, attempt + 1, exc)
                await asyncio.sleep(backoff_for(exc, attempt))
                continue

            if first is not None and first.text:
                yield first.text
            async for part in parts:
                if part.text:
                    yield part.text
            return

    raise UpstreamUnavailable(
        "Gemini is unavailable on every configured model (overload or rate limit) — "
        f"transient, retry shortly. Last error: {last}"
    )
