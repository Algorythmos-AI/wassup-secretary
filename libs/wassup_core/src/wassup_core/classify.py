"""Call classification: a generic rules engine over per-clinic vocabulary.

Which words mean "urgent" for an orthopaedic surgeon differ from a medspa, and those word lists are
each clinic's own (and, for some, a trade secret). So the *engine* lives here, in the open, and the
*rules* are data: one validated JSON document per clinic, stored versioned in the database and
never in this repository. The engine is pure and deterministic, so a rule set can be replayed over
a clinic's history and compared, and rolled back by version.

Inputs are what the voice provider gives us after a call: summary, transcript, intent, sentiment,
the agent's own triage route, and whether the provider judged the call successful. Everything is
matched case-insensitively as plain substrings; no regular expressions come from data.

Outcome:
- ``level``: ``emergency`` > ``priority_1`` > ``priority_2`` > ``priority_3`` > ``none``.
- ``is_priority``: the call needs attention before routine work (``emergency``, ``priority_1`` or
  ``priority_2``).
- ``is_reception_action``: somebody has to do something (call back, book, pass a message on).
- ``action_label`` / ``intent_label``: the words the dashboard shows for what the caller wanted.

Order of decision for the level, first match wins:
1. The agent's triage route, when the rules map it.
2. The rule set's tiers, in the order written. A tier is either a keyword list matched against the
   call's text, or the *priority signal* (negative sentiment, priority keywords, or a failed call
   that asked for something), which a clinic may place at whichever level it deserves for them.
3. ``none``.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Level = Literal["emergency", "priority_1", "priority_2", "priority_3", "none"]
LEVEL_RANK: dict[str, int] = {
    "emergency": 0,
    "priority_1": 1,
    "priority_2": 2,
    "priority_3": 3,
    "none": 4,
}
PRIORITY_LEVELS = frozenset({"emergency", "priority_1", "priority_2"})

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
MAX_KEYWORDS = 500


def _lower_unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        low = value.lower()
        if low not in seen:
            seen.append(low)
    if len(seen) > MAX_KEYWORDS:
        raise ValueError(f"at most {MAX_KEYWORDS} keywords per list")
    return seen


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KeywordTier(_Strict):
    """Any of these words in the call's text puts it at ``level``."""

    level: Level
    reason: Label
    any: list[Keyword] = Field(min_length=1)

    @field_validator("any")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return _lower_unique(v)


class SignalTier(_Strict):
    """The priority signal (see ``PrioritySignal``) puts the call at ``level``."""

    level: Level
    reason: Label
    signal: Literal["priority_call"]


class PrioritySignal(_Strict):
    """What counts as "this caller needs attention" independent of the tiers."""

    negative_sentiment: bool = True
    keywords: list[Keyword] = Field(default_factory=list)
    failed_call_with: list[Keyword] = Field(default_factory=list)

    @field_validator("keywords", "failed_call_with")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return _lower_unique(v)


class ReceptionActionRules(_Strict):
    failed_call: bool = True
    negative_sentiment: bool = True
    summary_any: list[Keyword] = Field(default_factory=list)
    transcript_any: list[Keyword] = Field(default_factory=list)
    intent_any: list[Keyword] = Field(default_factory=list)

    @field_validator("summary_any", "transcript_any", "intent_any")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return _lower_unique(v)


class ActionLabelRule(_Strict):
    """First rule that matches gives the label: a keyword in the intent or summary, a failed
    call, or the reception-action outcome itself (so "someone must follow up" can be a label)."""

    label: Label
    intent_any: list[Keyword] = Field(default_factory=list)
    summary_any: list[Keyword] = Field(default_factory=list)
    when_failed_call: bool = False
    when_reception_action: bool = False

    @field_validator("intent_any", "summary_any")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return _lower_unique(v)


class RuleSet(_Strict):
    """One clinic's vocabulary. ``schema_version`` lets the engine refuse rules it can't read."""

    schema_version: Literal[1] = 1
    route_levels: dict[Keyword, Level] = Field(default_factory=dict)
    tiers: list[KeywordTier | SignalTier] = Field(default_factory=list, max_length=20)
    priority_signal: PrioritySignal = Field(default_factory=PrioritySignal)
    reception_action: ReceptionActionRules = Field(default_factory=ReceptionActionRules)
    action_labels: list[ActionLabelRule] = Field(default_factory=list, max_length=50)
    default_action_label: Label = "Message Captured"
    intent_labels: dict[Keyword, Label] = Field(default_factory=dict)

    @field_validator("route_levels", "intent_labels")
    @classmethod
    def _lower_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        return {k.lower().strip(): val for k, val in v.items()}

    @classmethod
    def parse(cls, raw: str | bytes | dict[str, Any]) -> RuleSet:
        return cls.model_validate(raw if isinstance(raw, dict) else json.loads(raw))


class CallFacts(_Strict):
    """What we know about a call after analysis. Any field may be missing."""

    summary: str | None = None
    transcript: str | None = None
    intent: str | None = None
    sentiment: str | None = None
    triage_route: str | None = None
    call_successful: bool | None = None


class Classification(_Strict):
    level: Level
    reason: str
    is_priority: bool
    is_reception_action: bool
    action_label: str
    intent_label: str | None


def _contains_any(text: str | None, keywords: list[str]) -> bool:
    if not text or not keywords:
        return False
    low = text.lower()
    return any(k in low for k in keywords)


def _sentiment(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in ("positive", "negative", "neutral") else "unknown"


def _priority_signal(call: CallFacts, rules: PrioritySignal) -> bool:
    if rules.negative_sentiment and _sentiment(call.sentiment) == "negative":
        return True
    if _contains_any(call.summary, rules.keywords) or _contains_any(
        call.transcript, rules.keywords
    ):
        return True
    return call.call_successful is False and _contains_any(call.summary, rules.failed_call_with)


def _action_label(call: CallFacts, rules: RuleSet, reception_action: bool) -> str:
    for rule in rules.action_labels:
        if (
            _contains_any(call.intent, rule.intent_any)
            or _contains_any(call.summary, rule.summary_any)
            or (rule.when_failed_call and call.call_successful is False)
            or (rule.when_reception_action and reception_action)
        ):
            return rule.label
    return rules.default_action_label


def _intent_label(call: CallFacts, rules: RuleSet) -> str | None:
    if not call.intent:
        return None
    key = "_".join(call.intent.lower().strip().split())
    return rules.intent_labels.get(key, call.intent.strip() or None)


def _reception_action(call: CallFacts, rules: ReceptionActionRules) -> bool:
    if rules.failed_call and call.call_successful is False:
        return True
    if rules.negative_sentiment and _sentiment(call.sentiment) == "negative":
        return True
    return (
        _contains_any(call.summary, rules.summary_any)
        or _contains_any(call.transcript, rules.transcript_any)
        or _contains_any(call.intent, rules.intent_any)
    )


def _level(call: CallFacts, rules: RuleSet, action_label: str) -> tuple[str, str]:
    route = (call.triage_route or "").lower().strip()
    if route and route in rules.route_levels:
        return rules.route_levels[route], f"Agent triage: {route}"
    # The text a tier is matched against: everything the provider told us, plus the label we
    # derived, so a clinic can key a tier on "Appointment Request" without repeating its words.
    haystack = " ".join(
        s for s in (call.summary, call.transcript, call.intent, action_label, call.sentiment) if s
    ).lower()
    signal: bool | None = None
    for tier in rules.tiers:
        if isinstance(tier, KeywordTier):
            if any(k in haystack for k in tier.any):
                return tier.level, tier.reason
        else:
            if signal is None:
                signal = _priority_signal(call, rules.priority_signal)
            if signal:
                return tier.level, tier.reason
    return "none", ""


def classify(call: CallFacts, rules: RuleSet) -> Classification:
    reception_action = _reception_action(call, rules.reception_action)
    action_label = _action_label(call, rules, reception_action)
    level, reason = _level(call, rules, action_label)
    return Classification(
        level=level,
        reason=reason,
        is_priority=level in PRIORITY_LEVELS,
        is_reception_action=reception_action,
        action_label=action_label,
        intent_label=_intent_label(call, rules),
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m wassup_core.classify --rules rules.json --calls calls.json``: prints one JSON
    object per input call (same order), for parity checks against another implementation. The
    calls file is a JSON list of CallFacts objects, or ``-`` for standard input so call text need
    never touch a disk; nothing is written anywhere."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4 or args[0] != "--rules" or args[2] != "--calls":
        print("usage: --rules RULES.json --calls CALLS.json", file=sys.stderr)
        return 2
    with open(args[1], "rb") as f:
        rules = RuleSet.parse(f.read())
    if args[3] == "-":
        calls = json.load(sys.stdin)
    else:
        with open(args[3], "rb") as f:
            calls = json.load(f)
    for item in calls:
        result = classify(CallFacts.model_validate(item), rules)
        sys.stdout.write(json.dumps(result.model_dump(), separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
