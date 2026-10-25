"""Runtime configuration, all of it from the environment.

Model IDs live here rather than in code because Gemini model strings change
often and a wrong one is the most common first-day failure.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

RUNBOOK_DIR = Path(os.getenv("RUNBOOK_DIR", PROJECT_ROOT / "runbooks"))
STATIC_DIR = PROJECT_ROOT / "static"
PROMPTS_DIR = PROJECT_ROOT / "prompts"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PLACEHOLDER_KEYS = {"", "your_key_here", "your_gemini_api_key"}
GEMINI_KEY_PRESENT = GEMINI_API_KEY not in PLACEHOLDER_KEYS

QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
QDRANT_PATH = os.getenv("QDRANT_PATH", "./qdrant_data").strip()
COLLECTION = os.getenv("QDRANT_COLLECTION", "runbooks").strip()

EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001").strip()
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gemini-3.1-flash-lite").strip()
SYNTH_MODEL = os.getenv("SYNTH_MODEL", "gemini-3.8-flash").strip()

# Flash capacity is genuinely flaky — 503 "high demand" shows up on roughly one
# call in three. Retrying the same model then failing over keeps the pipeline usable.
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.1-flash-lite").split(",")
    if m.strip()
]

# The relevance floor, below which the judge is not called at all.
#
# Measured over the 87-query golden set (python -m evals.run_eval --sweep). The
# two populations overlap by 0.1145, so NO threshold separates them:
#
#   on-topic  selected-chunk cosine: 0.5418 .. 0.7886
#   off-topic selected-chunk cosine: 0.5638 .. 0.6562
#
# Every value is therefore a trade, not a solution. 0.62 is peak F1 (95.9) and is
# chosen deliberately:
#
#   0.52 -> 0 false refusals, 14 false accepts   (the floor filters NOTHING;
#                                                 every negative reaches a judge call)
#   0.62 -> 2 false refusals,  4 false accepts   F1 95.9
#   0.66 -> 8 false refusals,  0 false accepts
#
# The cost is real and named. At 0.62 two covered queries are refused without a
# judge call, and a floor refusal is final because it skips the judge that would
# have overturned it:
#
#   `oldest xmin`                             0.6181
#   `password_encryption md5 no longer accepted` 0.5418  <- SECTION 1 covers this
#
# That second one is a genuine capability loss: it is one of the three staleness
# cases the judge reliably catches, and at this floor it never gets the chance.
# Accepted because the alternative was a floor that refused nothing at all, and
# 8 of 12 off-topic queries now stop here instead of each costing a judge call.
#
# tests/test_pipeline.py holds the refusal count to a budget and reads it from
# evals/baseline.json, so raising this further fails the build rather than
# quietly trading away more coverage.
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.62"))

# Each finding costs a web search. The judge orders findings by severity, so the
# tail is the cheapest thing to drop.
MAX_SEARCHES = int(os.getenv("MAX_SEARCHES", "3"))

# Sized so each runbook stays ONE chunk. Measured, the corpus runs 2720-3431 chars
# per file; at the previous 2600 every single one overflowed and was split by the
# blank-line fallback in chunking.py, which yields fragments like a bare Prevention
# section under a "(continued)" heading. Such a fragment is incident-anonymous — it
# names no symptom — so retrieval matches it for the wrong logs and the judge is
# asked whether an excerpt covers an incident the excerpt never mentions.
#
# Raise this if a runbook legitimately grows past it. Do NOT lower it to force
# finer chunks: the unit the judge reasons about is one incident.
MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "3600"))

# Name of the BM25 sparse vector stored beside the dense one. Qdrant requires sparse
# vectors to be named even when the dense vector is not.
SPARSE_VECTOR = "bm25"

# Hybrid retrieval: dense and BM25 both searched, then fused. Off means dense only.
#
# Turning this on requires a collection ingested WITH sparse vectors — Qdrant cannot
# add a sparse config to an existing collection, so switching means re-running
# `python -m copilot.ingest`. The pipeline checks for the sparse vector at query time
# and falls back to dense rather than failing, so a stale collection degrades instead
# of erroring, but it will quietly give you dense-only results until you re-ingest.
HYBRID_SEARCH = os.getenv("HYBRID_SEARCH", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}

APP_API_KEY = os.getenv("APP_API_KEY", "").strip()
AUTH_ENABLED = bool(APP_API_KEY)
