"""Prompt loading.

Prompts live in prompts/*.md rather than in Python string literals. They are
content, not code: tuning them should not mean editing a module, and a reviewer
should be able to read them without scrolling past `\\`-continued lines.
"""

from functools import lru_cache

from .config import PROMPTS_DIR


@lru_cache(maxsize=None)
def load(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt '{name}' not found at {path}")
    return path.read_text(encoding="utf-8").strip()


JUDGE_SYSTEM = load("judge_system")
JUDGE_USER = load("judge_user")
SYNTH_SYSTEM = load("synth_system")
SYNTH_USER = load("synth_user")
MERGE_USER = load("merge_user")
WEAK_MATCH_NOTE = load("weak_match")

# The merge instructions extend the base rules rather than restating them, so
# a change to how commands are formatted only has to be made once.
MERGE_SYSTEM = f"{SYNTH_SYSTEM}\n\n{load('merge_system')}"
