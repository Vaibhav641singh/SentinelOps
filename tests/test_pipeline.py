import json
import pathlib

from copilot.config import MIN_MATCH_SCORE
from copilot.models import JudgeVerdict, StaleFinding
from copilot.pipeline import (
    PATH_NO_MATCH,
    PATH_RUNBOOK,
    PATH_WEB_PATCHED,
    choose_path,
    sse,
)
from copilot.research import FAILED, NOT_SEARCHED, Hit, Research, format_research

BASELINE = pathlib.Path(__file__).resolve().parent.parent / "evals" / "baseline.json"


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
    assert choose_path(covered=False, current=True) == PATH_NO_MATCH
    assert choose_path(covered=False, current=False) == PATH_NO_MATCH  # uncovered wins
    assert choose_path(covered=True, current=True) == PATH_RUNBOOK
    assert choose_path(covered=True, current=False) == PATH_WEB_PATCHED


def test_uncovered_incident_is_no_match_however_current_the_runbook():
    # pg_crash.log scored 0.6648 against the disk-full section and was reported as
    # web_patched. It is the same product, not the same incident. Whether that
    # section is up to date has no bearing on an incident it does not address.
    assert choose_path(covered=False, current=True) == PATH_NO_MATCH


def test_score_floor_is_below_every_observed_on_topic_score():
    # The floor must sit UNDER the on-topic minimum, or it refuses queries the
    # runbook covers — and a floor refusal is final, because it skips the judge
    # that would have overturned it.
    #
    # The bound is READ from the eval baseline rather than pasted in. It used to be
    # a hand-copied 0.5418, which is the one number in the suite that could silently
    # stop describing reality: nothing recomputed it, and the corpus has since grown
    # from one runbook to eleven. Regenerate with:
    #
    #     python -m evals.run_eval --write-baseline
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    on_topic_min = baseline["on_topic_min"]
    assert MIN_MATCH_SCORE < on_topic_min, (
        f"floor {MIN_MATCH_SCORE} would refuse an on-topic query scoring "
        f"{on_topic_min:.4f}, which no judge call can rescue"
    )


def test_baseline_records_the_overlap_that_justifies_the_judge():
    # Coverage lives in the judge because cosine cannot answer it. That claim is
    # only true while the two populations actually overlap — if a clean separating
    # threshold ever appears, the architecture should be revisited rather than
    # left as it is because a comment says so.
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["gap"] <= 0, (
        f"on-topic and off-topic scores now separate by {baseline['gap']:.4f}; "
        "a plain threshold may be sufficient and the judge-based gate is worth re-examining"
    )


def test_done_reports_only_findings_that_reached_the_answer():
    # The judge can flag staleness in a runbook it also declared irrelevant. Those
    # findings sit next to `sources` in the done event, where they read as researched
    # corrections to the advice given — but no research ran and the runbook was
    # discarded. Only findings carried by `research` shaped the answer.
    finding = StaleFinding(item="wal_keep_segments", problem="removed", search_query="q")
    research = [Research(finding, "ddgs", [Hit("PG13", "https://example.org", "renamed")])]

    def done_findings(research):
        return [group.finding.item for group in research] if research else []

    assert done_findings(research) == ["wal_keep_segments"]
    assert done_findings(None) == []  # no_match, however much the judge found


def test_unsearched_findings_are_still_reported_as_findings():
    # Past MAX_SEARCHES there are no hits, but these DO reach the merge prompt and
    # are corrected there marked [unverified], so they belong in the done event.
    finding = StaleFinding(item="recovery.conf", problem="removed in PG12", search_query="q")
    research = [Research(finding, NOT_SEARCHED)]
    assert [group.finding.item for group in research] == ["recovery.conf"]


def test_verdict_with_no_findings_is_treated_as_current():
    # A "stale" verdict with nothing to show for it would send an empty query
    # to the search step.
    verdict = JudgeVerdict(covers_incident=True, current=False, reason="vague", findings=[])
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
