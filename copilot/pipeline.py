"""The diagnosis pipeline, emitted as a stream of server-sent events.

Stage events exist so the caller can see which path ran — straight from the
runbook, or patched against the web. That visibility is the product.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import date

from qdrant_client import models

from .clients import async_qdrant_client, gemini_client
from .config import (
    COLLECTION,
    HYBRID_SEARCH,
    MAX_SEARCHES,
    MIN_MATCH_SCORE,
    SPARSE_VECTOR,
)
from .embeddings import QUERY, embed_async
from .judge import judge_runbook
from .models import JudgeVerdict
from .research import research_findings
from .sparse import encode_query
from .synthesis import synthesize_stream

log = logging.getLogger(__name__)

PATH_RUNBOOK = "runbook"
PATH_WEB_PATCHED = "web_patched"
PATH_NO_MATCH = "no_match"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


PREFETCH_DEPTH = 20


async def dense_score_of(qdrant, vector: list[float], point_id) -> float:
    """Cosine between the query and one specific point.

    Only needed when BM25 surfaces a chunk that dense retrieval ranked outside its
    top PREFETCH_DEPTH, so the cheap lookup table has no entry for it.
    """
    found = await qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        query_filter=models.Filter(must=[models.HasIdCondition(has_id=[point_id])]),
        limit=1,
    )
    return found.points[0].score if found.points else 0.0


async def retrieve(
    qdrant, vector: list[float], error_log: str, hybrid: bool
) -> tuple[object, float, str]:
    """Pick the best chunk, and score how well THAT chunk matched.

    Two different questions, so two different signals:

    - WHICH chunk is best is a ranking question, and fusing BM25 with dense answers it
      better than either alone (measured: dense 9/11, fused 11/11).
    - WHETHER it matched is an absolute question, and a fused score cannot answer it.
      Fusion scores are derived from rank, so the top result scores high by
      construction however irrelevant it is — measured, RRF returned exactly 1.0 for
      every query including ones no runbook covered. So the floor keeps reading raw
      dense cosine, which at least means something on its own.

    The score returned is the cosine of the chunk being returned, not of whatever
    dense retrieval happened to rank first. Those are frequently different points once
    fusion is involved, and a floor applied to one chunk while a different chunk is
    served is a threshold that describes nothing.

    Returns (best point, its dense cosine, retrieval mode).
    """
    if not hybrid:
        found = await qdrant.query_points(
            collection_name=COLLECTION, query=vector, limit=1, with_payload=True
        )
        if not found.points:
            return None, 0.0, "dense"
        return found.points[0], found.points[0].score, "dense"

    indices, values = encode_query(error_log)
    fused_query = qdrant.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=vector, using="", limit=PREFETCH_DEPTH),
            models.Prefetch(
                query=models.SparseVector(indices=indices, values=values),
                using=SPARSE_VECTOR,
                limit=PREFETCH_DEPTH,
            ),
        ],
        # DBSF normalizes each list's score distribution before combining, so BM25's
        # much larger spread still carries weight. RRF throws that away by reducing
        # every hit to its rank, and measured worse than sparse retrieval alone.
        query=models.FusionQuery(fusion=models.Fusion.DBSF),
        limit=1,
        with_payload=True,
    )
    # Same depth as the prefetch, so this doubles as a cosine lookup table for
    # whichever candidate fusion picks — usually avoiding a third round trip.
    dense_query = qdrant.query_points(
        collection_name=COLLECTION, query=vector, limit=PREFETCH_DEPTH, with_payload=False
    )
    fused, dense = await asyncio.gather(fused_query, dense_query)
    if not fused.points:
        return None, 0.0, "hybrid"

    best = fused.points[0]
    cosine = {point.id: point.score for point in dense.points}
    score = cosine.get(best.id)
    if score is None:
        score = await dense_score_of(qdrant, vector, best.id)
    return best, score, "hybrid"


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

        # A collection ingested before hybrid search existed has no sparse vector, and
        # a prefetch naming one would be rejected outright. Degrade to dense instead:
        # stale data should cost accuracy, not availability.
        params = (await qdrant.get_collection(COLLECTION)).config.params
        hybrid = HYBRID_SEARCH and bool(params.sparse_vectors)
        if HYBRID_SEARCH and not hybrid:
            log.warning(
                "HYBRID_SEARCH is on but collection %r has no %r vector — "
                "serving dense-only. Re-run: python -m copilot.ingest",
                COLLECTION,
                SPARSE_VECTOR,
            )

        vector = (await embed_async(gemini, [error_log], QUERY))[0]
        top, dense_score, mode = await retrieve(qdrant, vector, error_log, hybrid)
        if top is None:
            yield sse("error", {"message": "No runbook matched this log."})
            return

        payload = top.payload or {}
        chunk, source = payload["text"], payload["source"]
        weak = dense_score < MIN_MATCH_SCORE

        yield sse(
            "stage",
            {
                "stage": "retrieved",
                "source": source,
                # Still the dense cosine, not the fused score, so this number stays
                # comparable to every measurement taken before hybrid search existed.
                "score": round(dense_score, 4),
                "weak_match": weak,
                "mode": mode,
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
                # Only findings that reached the answer. The judge can return findings
                # about a runbook it also declared irrelevant; echoing those here next
                # to `sources` invites reading them as researched corrections of the
                # advice given, when no research ran and the runbook was discarded.
                # The full verdict is still on the `verdict` event for anyone who
                # wants it. Unsearched findings past MAX_SEARCHES are included,
                # because those do reach synthesis and are marked [unverified] there.
                "findings": [group.finding.item for group in research] if research else [],
                "sources": sources,
            },
        )

    except Exception as exc:
        log.exception("diagnosis failed")
        yield sse("error", {"message": f"{type(exc).__name__}: {exc}"})
    finally:
        await qdrant.close()
