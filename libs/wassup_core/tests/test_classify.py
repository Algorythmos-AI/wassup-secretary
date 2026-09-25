"""The classification engine, with synthetic vocabulary only (real clinic rules are data)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from wassup_core.classify import LEVEL_RANK, CallFacts, RuleSet, classify, main

RULES = RuleSet.parse(
    {
        "route_levels": {
            "emergency": "emergency",
            "urgent_text": "priority_2",
            "book": "priority_3",
        },
        "tiers": [
            {
                "level": "emergency",
                "reason": "Emergency language",
                "any": ["ambulance", "can't breathe"],
            },
            {"level": "priority_1", "reason": "Clinical attention", "any": ["fever", "bleeding"]},
            {
                "level": "priority_1",
                "reason": "Clinical concern for this clinic",
                "signal": "priority_call",
            },
            {"level": "priority_2", "reason": "Admin task", "any": ["reschedule", "referral"]},
            {
                "level": "priority_3",
                "reason": "Routine",
                "any": ["general enquiry", "opening hours"],
            },
            {"level": "priority_2", "reason": "Reception signal", "signal": "priority_call"},
        ],
        "priority_signal": {
            "negative_sentiment": True,
            "keywords": ["swelling", "wound", "Urgent"],
            "failed_call_with": ["callback", "message"],
        },
        "reception_action": {
            "summary_any": ["call back", "message"],
            "transcript_any": ["speak to"],
            "intent_any": ["booking", "prescription"],
        },
        "action_labels": [
            {"label": "Appointment Request", "intent_any": ["booking", "appointment"]},
            {"label": "Callback Needed", "summary_any": ["call back", "callback"]},
        ],
        "intent_labels": {"booking": "Appointment Request", "post_op": "Post-op Concern"},
    }
)


def _c(**facts: object) -> CallFacts:
    return CallFacts.model_validate(facts)


def test_agent_triage_route_wins_over_text() -> None:
    out = classify(_c(triage_route="Book", summary="ambulance needed"), RULES)
    assert (out.level, out.is_priority) == ("priority_3", False)
    assert out.reason == "Agent triage: book"


def test_tiers_apply_in_order_and_are_case_insensitive() -> None:
    assert classify(_c(summary="I CAN'T BREATHE"), RULES).level == "emergency"
    assert classify(_c(transcript="there is Bleeding"), RULES).level == "priority_1"
    assert classify(_c(summary="please reschedule"), RULES).level == "priority_2"
    assert classify(_c(summary="opening hours?"), RULES).level == "priority_3"
    assert classify(_c(summary="hello"), RULES).level == "none"


def test_priority_signal_sits_where_the_clinic_puts_it() -> None:
    # Swelling is a signal keyword: the first signal tier (priority_1) catches it.
    swelling = classify(_c(summary="some swelling after the operation"), RULES)
    assert (swelling.level, swelling.reason) == ("priority_1", "Clinical concern for this clinic")
    # Negative sentiment alone is the signal too.
    assert classify(_c(summary="hello", sentiment="Negative"), RULES).level == "priority_1"
    # A failed call that asked for a callback is the signal; a failed call that didn't is not.
    assert (
        classify(_c(summary="wants a callback", call_successful=False), RULES).level == "priority_1"
    )
    assert classify(_c(summary="hung up", call_successful=False), RULES).level == "none"


def test_tier_text_includes_the_derived_action_label() -> None:
    raw = RULES.model_dump()
    raw["tiers"] = [{"level": "priority_2", "reason": "Booking", "any": ["appointment request"]}]
    rules = RuleSet.parse(raw)
    assert classify(_c(intent="booking"), rules).level == "priority_2"


def test_reception_action_and_labels() -> None:
    out = classify(_c(intent="booking", summary="Wants to book."), RULES)
    assert out.is_reception_action is True
    assert out.action_label == "Appointment Request"
    assert out.intent_label == "Appointment Request"
    assert classify(_c(intent="Post Op"), RULES).intent_label == "Post-op Concern"
    assert classify(_c(intent="something_new"), RULES).intent_label == "something_new"
    assert classify(_c(summary="nothing much"), RULES).action_label == "Message Captured"
    assert classify(_c(call_successful=False), RULES).is_reception_action is True
    assert (
        classify(
            _c(summary="chat", sentiment="positive", call_successful=True), RULES
        ).is_reception_action
        is False
    )


def test_action_labels_can_key_on_a_failed_call_or_the_follow_up_outcome() -> None:
    raw = RULES.model_dump()
    raw["action_labels"] = [
        {"label": "Reception Action", "when_failed_call": True},
        {"label": "Team Follow-up", "when_reception_action": True},
    ]
    raw["default_action_label"] = "No Action"
    rules = RuleSet.parse(raw)
    assert classify(_c(call_successful=False), rules).action_label == "Reception Action"
    assert classify(_c(summary="please call back"), rules).action_label == "Team Follow-up"
    assert (
        classify(_c(summary="just chatting", call_successful=True), rules).action_label
        == "No Action"
    )


def test_is_priority_means_emergency_or_priority_1_or_2() -> None:
    for text, expected in (
        ("ambulance", True),
        ("fever", True),
        ("reschedule", True),
        ("opening hours", False),
        ("x", False),
    ):
        assert classify(_c(summary=text), RULES).is_priority is expected


def test_empty_rules_classify_nothing() -> None:
    out = classify(
        _c(summary="ambulance fever swelling", sentiment="negative", call_successful=False),
        RuleSet(),
    )
    assert (out.level, out.is_priority) == ("none", False)
    assert out.is_reception_action is True  # failed call and negative sentiment are on by default


@pytest.mark.parametrize(
    "bad",
    [
        {"tiers": [{"level": "critical", "reason": "x", "any": ["a"]}]},  # unknown level
        {"tiers": [{"level": "priority_1", "reason": "x", "any": []}]},  # empty keyword list
        {
            "tiers": [{"level": "priority_1", "reason": "x", "any": ["a"], "regex": ".*"}]
        },  # extra key
        {"schema_version": 2},
        {"tiers": [{"level": "priority_1", "reason": "x", "any": ["k" * 81]}]},
        {"priority_signal": {"keywords": [f"k{i}" for i in range(501)]}},
    ],
)
def test_invalid_rules_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        RuleSet.parse(bad)


def test_rules_round_trip_through_json() -> None:
    again = RuleSet.parse(json.dumps(RULES.model_dump()))
    assert again == RULES


# --- properties -------------------------------------------------------------------------------

texts = st.text(min_size=0, max_size=200)
facts = st.builds(
    CallFacts,
    summary=st.one_of(st.none(), texts),
    transcript=st.one_of(st.none(), texts),
    intent=st.one_of(st.none(), texts),
    sentiment=st.sampled_from([None, "Positive", "Negative", "Neutral", "odd"]),
    triage_route=st.sampled_from([None, "emergency", "urgent_text", "book", "unknown", "BOOK"]),
    call_successful=st.sampled_from([None, True, False]),
)


@settings(max_examples=300)
@given(facts)
def test_deterministic_and_case_insensitive(call: CallFacts) -> None:
    first = classify(call, RULES)
    assert classify(call, RULES) == first
    upper = CallFacts(
        summary=call.summary.upper() if call.summary else None,
        transcript=call.transcript.upper() if call.transcript else None,
        intent=call.intent.upper() if call.intent else None,
        sentiment=call.sentiment,
        triage_route=call.triage_route,
        call_successful=call.call_successful,
    )
    shouted = classify(upper, RULES)
    assert (
        shouted.level,
        shouted.is_priority,
        shouted.is_reception_action,
        shouted.action_label,
    ) == (
        first.level,
        first.is_priority,
        first.is_reception_action,
        first.action_label,
    )


@settings(max_examples=300)
@given(facts, st.sampled_from(["ambulance", "fever", "reschedule", "opening hours"]))
def test_adding_a_keyword_never_makes_a_call_less_urgent(call: CallFacts, word: str) -> None:
    before = classify(call, RULES)
    louder = call.model_copy(update={"summary": f"{call.summary or ''} {word}"})
    after = classify(louder, RULES)
    if call.triage_route and call.triage_route.lower() in RULES.route_levels:
        assert after.level == before.level  # the agent's route is authoritative
    else:
        assert LEVEL_RANK[after.level] <= LEVEL_RANK[before.level]


@settings(max_examples=200)
@given(facts)
def test_is_priority_is_exactly_a_function_of_level(call: CallFacts) -> None:
    out = classify(call, RULES)
    assert out.is_priority == (out.level in ("emergency", "priority_1", "priority_2"))


def test_cli_prints_one_result_per_call(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    d = Path(str(tmp_path))
    (d / "rules.json").write_text(json.dumps(RULES.model_dump()))
    (d / "calls.json").write_text(json.dumps([{"summary": "ambulance"}, {"summary": "hi"}]))
    assert main(["--rules", str(d / "rules.json"), "--calls", str(d / "calls.json")]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["level"] for line in lines] == ["emergency", "none"]
    assert main(["--nope"]) == 2


def test_cli_reads_calls_from_stdin(
    tmp_path: object, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    d = Path(str(tmp_path))
    (d / "rules.json").write_text(json.dumps(RULES.model_dump()))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"summary": "fever"}])))
    assert main(["--rules", str(d / "rules.json"), "--calls", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["level"] == "priority_1"
