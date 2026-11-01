"""Judge-as-reranker: re-order the top candidates with the model already in the pipeline.

The pipeline serves DBSF's top-1. Every remaining top-1 miss on the golden set is
a near-neighbour confusion — `Exit Code: 137` landing on crashloopbackoff rather
than oomkilled — which is exactly the case a cross-encoder is supposed to fix,
because the distinction is semantic and both documents share most of their
vocabulary.

The reranker here is `judge_runbook`, unchanged. It already reads (log, chunk)
and returns `covers_incident`, which IS a cross-encoder relevance decision; it
just only ever sees one chunk. Showing it three and keeping the ones it accepts
costs no new dependency, no new API key, and no new prompt — and because it is
the shipped judge, what this measures is what would actually run.

A local cross-encoder (bge-reranker, ms-marco-MiniLM) is the conventional answer
and is ruled out by the same constraint that ruled out fastembed: it needs torch,
which is far past the image budget for a free-plan deployment.

COST: one judge call per candidate. Verdicts are cached on (query, chunk) so a
re-run is free, but the first pass over the full golden set at depth 3 is ~260
calls. Use --rerank-limit to bound it.
"""

import asyncio
import hashlib
import json
import logging
import pathlib

from copilot.judge import judge_runbook

log = logging.getLogger("evals.rerank")

CACHE = pathlib.Path(__file__).resolve().parent / "cache" / "judge_verdicts.json"

# Gemini flash-lite tolerates this comfortably and it keeps a full run to a few
# minutes without tripping the rate limit that would trigger failover.
CONCURRENCY = 4


def verdict_key(query: str, chunk: str) -> str:
    return hashlib.sha256(f"{query}\x00{chunk}".encode()).hexdigest()


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")


async def judge_candidates(
    gemini, query: str, candidates: list[dict], cache: dict, today: str
) -> list[bool]:
    """Ask `covers_incident` for each candidate. Returns one bool per candidate."""
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(candidate: dict) -> bool:
        key = verdict_key(query, candidate["text"])
        if key in cache:
            return cache[key]
        async with semaphore:
            verdict = await judge_runbook(
                gemini, query, candidate["text"], candidate["source"], today, "evalnonce"
            )
        cache[key] = bool(verdict.covers_incident)
        return cache[key]

    return await asyncio.gather(*(one(c) for c in candidates))


def reorder(ranked: list[str], accepted: list[bool]) -> list[str]:
    """Stable partition: accepted candidates first, fusion order preserved within each.

    Deliberately not a re-scoring. `covers_incident` is a boolean, so there is no
    score to sort on, and inventing one (asking the judge for a 1-10 relevance
    number) would mean a different prompt and a different judge than the one that
    ships. A stable partition changes the answer only where the judge actively
    disagrees with fusion, which is the smallest intervention that can help.

    When the judge accepts all of them or none of them, the order is unchanged —
    and that is the common case, which is itself a finding.
    """
    keep = [key for key, ok in zip(ranked, accepted, strict=True) if ok]
    drop = [key for key, ok in zip(ranked, accepted, strict=True) if not ok]
    return keep + drop
