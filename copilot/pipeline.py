"""The diagnosis pipeline, emitted as a stream of server-sent events.

Stage events exist so the caller can see which path ran — straight from the
runbook, or patched against the web. That visibility is the product.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import date

from .clients import async_qdrant_client, gemini_client
from .config import COLLECTION, MAX_SEARCHES, MIN_MATCH_SCORE
from .embeddings import QUERY, embed_async
from .judge import judge_runbook
from .models import JudgeVerdict
from .research import research_findings
from .synthesis import synthesize_stream

log = logging.getLogger(__name__)

PATH_RUNBOOK = "runbook"
PATH_WEB_PATCHED = "web_patched"
PATH_NO_MATCH = "no_match"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def choose_path(covered: bool, current: bool) -> str:
    """Which of the three answers the caller is getting.

    `covered` folds together both ways the library can fail to cover an incident:
    the score floor, and the judge's own reading of the excerpt.
    """
    if not covered:
        return PATH_NO_MATCH
    return PATH_RUNBOOK if current else PATH_WEB_PATCHED


async def diagnose_events(error_log: str) -> AsyncIterator[str]:
    """Run the pipeline, yielding one SSE frame per stage.

    Every failure becomes an `error` event rather than an exception: once the
    response has started, the status code is already sent, so raising would cut
    the stream dead with no explanation for the caller.
    """
    qdrant = async_qdrant_client()
    try:
        gemini = gemini_client()

        yield sse("stage", {"stage": "retrieving"})
        if not await qdrant.collection_exists(COLLECTION):
            yield sse(
                "error",
                {"message": f"Collection '{COLLECTION}' is missing. Run: python -m copilot.ingest"},
            )
            return

        vector = (await embed_async(gemini, [error_log], QUERY))[0]
        found = await qdrant.query_points(
            collection_name=COLLECTION, query=vector, limit=1, with_payload=True
        )
        if not found.points:
            yield sse("error", {"message": "No runbook matched this log."})
            return

        top = found.points[0]
        payload = top.payload or {}
        chunk, source = payload["text"], payload["source"]
        weak = top.score < MIN_MATCH_SCORE

        yield sse(
            "stage",
            {
                "stage": "retrieved",
                "source": source,
                "score": round(top.score, 4),
                "weak_match": weak,
            },
        )

        verdict = JudgeVerdict(
            covers_incident=False,
            current=True,
            reason="Skipped: no runbook matched closely enough.",
        )
        if weak:
            # Below the floor the chunk is obvious garbage, not worth a judge call.
            # Synthesis still runs so the refusal is the model's own.
            yield sse(
                "stage",
                {"stage": "skipped_judge", "why": "weak_match", "threshold": MIN_MATCH_SCORE},
            )
        else:
            yield sse("stage", {"stage": "judging"})
            verdict = await judge_runbook(
                gemini, error_log, chunk, source, date.today().isoformat()
            )
            yield sse("verdict", verdict.model_dump())

        # Coverage is the judge's call above the floor. Cosine cannot answer it:
        # measured, on-topic and off-topic scores overlap (see config.MIN_MATCH_SCORE).
        covered = not weak and verdict.covers_incident
        if not weak and not covered:
            yield sse(
                "stage",
                {"stage": "not_covered", "why": "judge", "reason": verdict.reason},
            )

        research = None
        sources: list[str] = []
        if covered and not verdict.current:
            yield sse(
                "stage",
                {
                    "stage": "web_search",
                    "findings": len(verdict.findings),
                    "searching": [f.search_query for f in verdict.findings[:MAX_SEARCHES]],
                },
            )
            research, sources = await research_findings(gemini, verdict.findings)
            yield sse(
                "stage",
                {
                    "stage": "web_results",
                    "per_finding": [
                        {"item": g.finding.item, "backend": g.backend, "hits": len(g.hits)}
                        for g in research
                    ],
                    "sources": sources,
                },
            )

        path = choose_path(covered, verdict.current)
        yield sse("stage", {"stage": "synthesizing", "path": path})

        async for text in synthesize_stream(
            gemini, error_log, chunk, source, research=research, weak=not covered
        ):
            yield sse("token", {"text": text})

        yield sse(
            "done",
            {
                "path": path,
                "source": source,
                "findings": [f.item for f in verdict.findings],
                "sources": sources,
            },
        )

    except Exception as exc:
        log.exception("diagnosis failed")
        yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        await qdrant.close()
