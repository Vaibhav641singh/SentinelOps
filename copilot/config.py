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

# A conservative floor, not a relevance classifier. Below this the chunk is obvious
# garbage and not worth a judge call; everything above it goes to the judge, which
# decides coverage via JudgeVerdict.covers_incident.
#
# This used to be 0.62 and was asked to decide relevance on its own. It cannot.
# Measured over 16 queries (scripts/measure_hybrid.py), the two populations overlap:
#
#   on-topic  dense top1: 0.5602 .. 0.7706
#   off-topic dense top1: 0.5621 .. 0.6648   <- overlaps by 0.1046
#
# No threshold separates them. The best any single value achieves is 14/16, which
# 0.62 already hit, so tuning the number was exhausted. The two failures are
# semantic: pg_crash.log scores 0.6648 because it is *about Postgres*, not because
# the disk-full section fixes it, while a short on-topic query scored 0.5602 purely
# because short queries sit lower against long chunks. Cosine measures topic, not
# coverage, so coverage moved to the judge and this dropped to a floor that only
# catches the genuinely unrelated.
#
# Re-derived at 0.52 once the score started describing the chunk actually served
# rather than whatever dense retrieval ranked first (see pipeline.retrieve). Those
# are different points under fusion, and the chunk-level scores run lower:
#
#   on-topic  selected-chunk cosine: 0.5418 .. 0.7706
#   off-topic selected-chunk cosine: 0.4981 .. 0.6644
#
# 0.55 would now refuse `password_encryption md5 no longer accepted` at 0.5418,
# which SECTION 1 covers on line 60. The floor must stay BELOW the lowest on-topic
# score — it is a garbage filter, and every false refusal it causes is one the judge
# never gets the chance to overturn.
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.52"))

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
