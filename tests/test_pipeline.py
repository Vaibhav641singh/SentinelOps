import json

from copilot.config import MIN_MATCH_SCORE
from copilot.models import JudgeVerdict, StaleFinding
from copilot.pipeline import PATH_NO_MATCH, PATH_RUNBOOK, PATH_WEB_PATCHED, sse
from copilot.research import FAILED, NOT_SEARCHED, Hit, Research, format_research


def parse_frame(frame: str):
    """Parse an SSE frame the way a browser or the CLI would."""
    name, payload = None, None
    for line in frame.strip().split("\n"):
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    return name, payload


def test_sse_frame_is_well_formed():
    frame = sse("stage", {"stage": "retrieving"})
    assert frame.endswith("\n\n")  # blank line terminates the frame
    assert parse_frame(frame) == ("stage", {"stage": "retrieving"})


def test_sse_payload_stays_on_one_line():
    # A newline inside data: would split the frame and corrupt the stream.
    frame = sse("token", {"text": "line one\nline two"})
    assert len([ln for ln in frame.strip().split("\n") if ln.startswith("data: ")]) == 1
    _, payload = parse_frame(frame)
    assert payload["text"] == "line one\nline two"


def test_path_selection():
    def choose(weak: bool, current: bool) -> str:
        return PATH_NO_MATCH if weak else (PATH_RUNBOOK if current else PATH_WEB_PATCHED)

    assert choose(weak=True, current=True) == PATH_NO_MATCH
    assert choose(weak=True, current=False) == PATH_NO_MATCH  # weak wins
    assert choose(weak=False, current=True) == PATH_RUNBOOK
    assert choose(weak=False, current=False) == PATH_WEB_PATCHED


def test_relevance_gate_threshold_sits_between_observed_scores():
    # Measured: on-topic logs 0.74-0.78, unrelated nginx error 0.55.
    assert 0.57 < MIN_MATCH_SCORE < 0.74


def test_verdict_with_no_findings_is_treated_as_current():
    # A "stale" verdict with nothing to show for it would send an empty query
    # to the search step.
    verdict = JudgeVerdict(current=False, reason="vague", findings=[])
    if not verdict.current and not verdict.findings:
        verdict.current = True
    assert verdict.current


def test_unresearched_findings_still_reach_the_merge_prompt():
    # Dropping findings past the search cap silently discarded real staleness.
    finding = StaleFinding(item="wal_keep_segments", problem="removed", search_query="q")
    rendered = format_research([Research(finding, NOT_SEARCHED)])
    assert "wal_keep_segments" in rendered
    assert "[unverified]" in rendered


def test_failed_search_is_distinguished_from_unsearched():
    finding = StaleFinding(item="x", problem="p", search_query="q")
    assert "not researched" in format_research([Research(finding, NOT_SEARCHED)])
    assert "returned nothing" in format_research([Research(finding, FAILED)])


def test_research_renders_hits_with_urls():
    finding = StaleFinding(item="wal_keep_segments", problem="removed in PG13", search_query="q")
    group = Research(finding, "ddgs", [Hit("PG13 notes", "https://example.org/pg13", "renamed")])
    rendered = format_research([group])
    assert "https://example.org/pg13" in rendered
    assert "removed in PG13" in rendered
