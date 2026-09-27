import math

from copilot.chunking import chunk_runbook

RUNBOOK = """INTERNAL RUNBOOK
Last reviewed: 2023

SECTION 1 — first problem

Steps go here.

SECTION 2 — second problem

More steps.
"""


def test_splits_one_chunk_per_section_plus_header():
    chunks = chunk_runbook(RUNBOOK)
    assert len(chunks) == 3
    assert chunks[0].startswith("INTERNAL RUNBOOK")
    assert chunks[1].startswith("SECTION 1")
    assert chunks[2].startswith("SECTION 2")


def test_keeps_a_section_whole_when_it_fits():
    # The section is the unit the judge reasons about, so it must not be split
    # while it fits under the limit.
    chunks = chunk_runbook(RUNBOOK, max_chars=2600)
    section_one = next(c for c in chunks if c.startswith("SECTION 1"))
    assert "Steps go here." in section_one


def test_oversized_section_splits_and_keeps_its_heading():
    big = "SECTION 1 — huge\n\n" + "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(10))
    chunks = chunk_runbook(big, max_chars=500)
    assert len(chunks) > 1
    assert all(c.startswith("SECTION 1") for c in chunks)


def test_no_content_is_lost():
    chunks = chunk_runbook(RUNBOOK)
    joined = " ".join(chunks)
    for marker in ("INTERNAL RUNBOOK", "Steps go here.", "More steps."):
        assert marker in joined


def test_document_without_sections_survives():
    chunks = chunk_runbook("just some text with no headings")
    assert chunks == ["just some text with no headings"]


def test_empty_input_yields_nothing():
    assert chunk_runbook("") == []
