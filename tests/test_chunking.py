import pytest

from copilot.chunking import chunk_runbook
from copilot.config import MAX_CHUNK_CHARS, RUNBOOK_DIR
from copilot.ingest import METADATA_KEYS, parse_metadata

MARKDOWN_RUNBOOKS = sorted(RUNBOOK_DIR.glob("*.md"))

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


# --- the real corpus -------------------------------------------------------
#
# These read runbooks/ rather than a fixture on purpose. The failure they guard
# against is a config change, not a code change: at MAX_CHUNK_CHARS=2600 every
# .md runbook overflowed and split into fragments like a bare Prevention section
# under a "(continued)" heading, which names no symptom and so is retrievable for
# the wrong incident. Nothing in the code would have caught that.


@pytest.mark.parametrize("path", MARKDOWN_RUNBOOKS, ids=lambda p: p.name)
def test_each_markdown_runbook_is_exactly_one_chunk(path):
    text = path.read_text(encoding="utf-8-sig")
    chunks = chunk_runbook(text)
    assert len(chunks) == 1, (
        f"{path.name} split into {len(chunks)} chunks at MAX_CHUNK_CHARS="
        f"{MAX_CHUNK_CHARS} ({len(text)} chars). One incident is one chunk; "
        "raise the limit rather than shipping fragments."
    )


@pytest.mark.parametrize("path", MARKDOWN_RUNBOOKS, ids=lambda p: p.name)
def test_each_markdown_runbook_carries_full_metadata(path):
    meta = parse_metadata(path.read_text(encoding="utf-8-sig"))
    assert set(meta) == set(METADATA_KEYS), f"{path.name} is missing {METADATA_KEYS - set(meta)}"


def test_metadata_ignores_prose_headers():
    # database_guide.txt opens with "Owner: Platform Team" and "Applies to: ...",
    # which match the key:value shape but are not metadata. The whitelist is what
    # keeps them out of the payload.
    legacy = (RUNBOOK_DIR / "database_guide.txt").read_text(encoding="utf-8-sig")
    assert parse_metadata(legacy) == {}


def test_metadata_stops_at_the_first_section():
    # Below a heading the same shape means something else: a Diagnosis step reads
    # `state: 'idle in transaction'`, a Symptoms block quotes `Reason: OOMKilled`.
    text = "# Title\n\nservice: postgres\n\n## Diagnosis\n\ncomponent: not-metadata\n"
    assert parse_metadata(text) == {"service": "postgres"}
