"""Splitting runbooks into the units that get embedded and judged."""

import re

from .config import MAX_CHUNK_CHARS

SECTION_BOUNDARY = re.compile(r"\n(?=SECTION\b)")
BLANK_LINE = re.compile(r"\n\s*\n")


def chunk_runbook(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split on SECTION headings, keeping each section whole where possible.

    The section is the unit the judge reasons about, so splitting mid-section
    would hand it half an argument to rule on. Only oversized sections are
    broken further, and each part keeps its heading so it still reads in context.
    """
    header, *rest = SECTION_BOUNDARY.split(text.strip())
    blocks = [header.strip(), *(s.strip() for s in rest)] if rest else [header.strip()]

    chunks: list[str] = []
    for block in blocks:
        if not block:
            continue
        if len(block) <= max_chars:
            chunks.append(block)
            continue

        heading = block.splitlines()[0]
        current = ""
        for para in BLANK_LINE.split(block):
            if current and len(current) + len(para) > max_chars:
                chunks.append(current.strip())
                current = f"{heading}\n(continued)\n\n{para}"
            else:
                current = f"{current}\n\n{para}" if current else para
        if current.strip():
            chunks.append(current.strip())

    return chunks
