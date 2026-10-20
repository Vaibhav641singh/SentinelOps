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
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.55"))

# Each finding costs a web search. The judge orders findings by severity, so the
# tail is the cheapest thing to drop.
MAX_SEARCHES = int(os.getenv("MAX_SEARCHES", "3"))

MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "2600"))

APP_API_KEY = os.getenv("APP_API_KEY", "").strip()
AUTH_ENABLED = bool(APP_API_KEY)
