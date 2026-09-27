"""Web research for findings the judge flagged.

DuckDuckGo is the primary backend; Gemini's own search grounding is the
fallback. `ddgs` returns 202/403/429 often enough that a single backend would
make the self-correction path unreliable, and that path is the whole product.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from .clients import with_failover
from .config import MAX_SEARCHES, SYNTH_MODEL
from .models import StaleFinding

log = logging.getLogger(__name__)

DDG = "ddgs"
GROUNDING = "gemini_grounding"
FAILED = "failed"
NOT_SEARCHED = "not_searched"


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


@dataclass
class Research:
    """What was found for one finding, including when nothing was."""

    finding: StaleFinding
    backend: str
    hits: list[Hit] = field(default_factory=list)


def _ddg_search(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    return DDGS().text(query=query, max_results=max_results)


async def web_search(gemini: genai.Client, query: str, max_results: int = 5) -> tuple[str, list[Hit]]:
    from ddgs.exceptions import DDGSException

    try:
        # ddgs is blocking; calling it directly would stall the event loop.
        raw = await asyncio.to_thread(_ddg_search, query, max_results)
        hits = [
            Hit(r.get("title", ""), r.get("href", ""), r.get("body", ""))
            for r in raw
        ]
        if hits:
            return DDG, hits
    except (DDGSException, OSError) as exc:
        log.warning("ddgs failed for %r, falling back to grounding: %s", query, exc)

    async def call(model: str):
        return await gemini.aio.models.generate_content(
            model=model,
            contents=f"Search the web and summarise the current guidance for: {query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )

    response = await with_failover(SYNTH_MODEL, call)
    hits = [Hit("Gemini web grounding", "", response.text or "")]
    for candidate in response.candidates or []:
        meta = candidate.grounding_metadata
        for grounded in (meta.grounding_chunks or []) if meta else []:
            if grounded.web and grounded.web.uri:
                hits.append(Hit(grounded.web.title or "", grounded.web.uri, ""))
    return GROUNDING, hits


async def research_findings(
    gemini: genai.Client, findings: list[StaleFinding]
) -> tuple[list[Research], list[str]]:
    """One targeted search per finding, run concurrently and capped.

    Findings past the cap are still returned with no hits: dropping them would
    silently discard real staleness the judge did find, and the merge step is
    told to correct those from model knowledge and mark them [unverified].
    """
    picked = findings[:MAX_SEARCHES]
    results = await asyncio.gather(
        *(web_search(gemini, f.search_query) for f in picked), return_exceptions=True
    )

    groups: list[Research] = []
    seen: set[str] = set()
    sources: list[str] = []

    for finding, result in zip(picked, results):
        if isinstance(result, BaseException):
            log.warning("research failed for %r: %s", finding.item, result)
            groups.append(Research(finding, FAILED))
            continue
        backend, hits = result
        groups.append(Research(finding, backend, hits))
        for hit in hits:
            if hit.url and hit.url not in seen:
                seen.add(hit.url)
                sources.append(hit.url)

    groups.extend(Research(f, NOT_SEARCHED) for f in findings[MAX_SEARCHES:])
    return groups, sources


def format_research(groups: list[Research]) -> str:
    """Render research as the numbered block the merge prompt expects."""
    blocks = []
    for n, group in enumerate(groups, 1):
        if group.hits:
            web = "\n".join(
                f"    - {h.title}\n      {h.url}\n      {h.snippet[:400]}" for h in group.hits
            )
        elif group.backend == NOT_SEARCHED:
            web = "    (not researched — correct from your own knowledge, mark [unverified])"
        else:
            web = "    (search returned nothing — correct from your own knowledge, mark [unverified])"
        blocks.append(
            f"FINDING {n}: {group.finding.item}\n"
            f"  problem: {group.finding.problem}\n"
            f"  web results ({group.backend}):\n{web}"
        )
    return "\n\n".join(blocks)
