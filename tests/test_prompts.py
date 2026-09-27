from copilot.prompts import (
    JUDGE_SYSTEM,
    JUDGE_USER,
    MERGE_SYSTEM,
    MERGE_USER,
    SYNTH_SYSTEM,
    SYNTH_USER,
)
from copilot.synthesis import build_prompt


def test_every_prompt_loaded():
    for prompt in (JUDGE_SYSTEM, JUDGE_USER, SYNTH_SYSTEM, SYNTH_USER, MERGE_SYSTEM, MERGE_USER):
        assert prompt.strip()


def test_judge_prompt_accepts_todays_date():
    assert "{today}" in JUDGE_SYSTEM
    assert "{today}" not in JUDGE_SYSTEM.format(today="2026-09-28")


def test_merge_extends_the_base_rules_rather_than_restating_them():
    assert MERGE_SYSTEM.startswith(SYNTH_SYSTEM)
    assert "OUTDATED" in MERGE_SYSTEM


def test_plain_path_uses_the_base_prompt():
    system, user = build_prompt("some log", "some chunk", "guide.txt")
    assert system == SYNTH_SYSTEM
    assert "some log" in user and "some chunk" in user


def test_weak_match_warns_the_model_before_the_excerpt():
    _, user = build_prompt("log", "chunk", "guide.txt", weak=True)
    assert "does not cover this incident" in user
    assert user.index("does not cover") < user.index("chunk")


def test_no_placeholder_survives_formatting():
    for _, user in (
        build_prompt("log", "chunk", "guide.txt"),
        build_prompt("log", "chunk", "guide.txt", weak=True),
    ):
        assert "{error_log}" not in user
        assert "{chunk}" not in user
        assert "{source}" not in user
