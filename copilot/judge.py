"""The staleness audit — the idea the whole service exists for."""

from google import genai
from google.genai import types

from .clients import with_failover
from .config import JUDGE_MODEL
from .models import JudgeVerdict
from .prompts import JUDGE_SYSTEM, JUDGE_USER


async def judge_runbook(
    gemini: genai.Client, error_log: str, chunk: str, source: str, today: str
) -> JudgeVerdict:
    """Ask whether a retrieved runbook excerpt is still safe to follow.

    Returns a schema-validated verdict so the caller branches on a boolean
    rather than parsing prose.
    """

    async def call(model: str):
        return await gemini.aio.models.generate_content(
            model=model,
            contents=JUDGE_USER.format(error_log=error_log, source=source, chunk=chunk),
            config=types.GenerateContentConfig(
                system_instruction=JUDGE_SYSTEM.format(today=today),
                response_mime_type="application/json",
                response_schema=JudgeVerdict,
                # Thinking is left at the model default on purpose. Disabling it
                # (thinking_budget=0) measurably degraded factual accuracy: naming
                # the correct replacement for a removed parameter fell from 9/10 to
                # 8/13, and it was not even faster. Prompt wording did not fix this;
                # the thinking budget did. Do not "optimise" this back.
            ),
        )

    response = await with_failover(JUDGE_MODEL, call)
    verdict = response.parsed

    if not isinstance(verdict, JudgeVerdict):
        # An unreadable verdict degrades to plain runbook advice rather than
        # firing a pointless web search.
        return JudgeVerdict(current=True, reason="Judge returned no usable verdict.")

    # A verdict claiming staleness with nothing to show for it would send an
    # empty query to the search step, so trust the findings over the boolean.
    if not verdict.current and not verdict.findings:
        verdict.current = True

    return verdict
