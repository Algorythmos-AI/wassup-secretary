"""One way to write a phone number, so two numbers can be compared for equality.

``canonical_phone`` returns E.164 (``+61412345678``) or ``None`` when the input is not a usable
subscriber number: empty, a withheld-caller-ID marker (Twilio's ``+266696687`` spells
ANONYMOUS; providers also send words like "anonymous" or "restricted"), or a shape this cannot
place with confidence. Nothing is guessed: an unplaceable number compares equal to nothing,
which is the safe answer for both a caller-ID check and a rate limit.

Australian numbers are the home case: ``04xx``, ``(02)``, ``+61 (0)4``, ``0011 61``, and a
nine-digit number missing its trunk zero. Anything else must arrive with ``+`` and its country
code. Extensions (``x123``, ``ext 4``) are dropped.
"""

from __future__ import annotations

import re

WITHHELD_WORDS = frozenset(
    {"anonymous", "restricted", "unavailable", "private", "unknown", "withheld", "blocked"}
)
WITHHELD_DIGITS = frozenset({"266696687"})  # Twilio's ANONYMOUS sentinel (+266696687)
_EXTENSION = re.compile(r"\s*(?:x|ext|extension)\.?\s*\d*\s*$|#.*$", re.IGNORECASE)


def canonical_phone(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw or raw.lower().lstrip("+") in WITHHELD_WORDS:
        return None
    raw = _EXTENSION.sub("", raw)
    international = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits or digits in WITHHELD_DIGITS:
        return None
    if not international and digits.startswith("0011"):
        international, digits = True, digits[4:]
    if international:
        return _international(digits)
    if digits.startswith("0"):
        digits = digits[1:]
    return f"+61{digits}" if _australian_subscriber(digits) else None


def _international(digits: str) -> str | None:
    if digits.startswith("61"):
        rest = digits[2:]
        if rest.startswith("0"):
            rest = rest[1:]  # +61 (0)4xx: the trunk zero is not dialled internationally
        return f"+61{rest}" if _australian_subscriber(rest) else None
    if digits.startswith("0") or not 7 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def _australian_subscriber(digits: str) -> bool:
    """Nine digits, area/mobile prefix 2 to 8 (13xx, 1300 and 1800 numbers are not subscribers)."""
    return len(digits) == 9 and digits[0] in "2345678" and digits.isdigit()
