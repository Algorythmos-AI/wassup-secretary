"""One canonical form for phone numbers; nothing is guessed."""

from __future__ import annotations

import pytest
from wassup_core.phone import canonical_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0412 345 678", "+61412345678"),
        ("+61 412 345 678", "+61412345678"),
        ("+61 (0)412 345 678", "+61412345678"),
        ("0011 61 412 345 678", "+61412345678"),
        ("412345678", "+61412345678"),  # trunk zero missing
        ("(02) 9876 5432", "+61298765432"),
        ("08 7123 4567", "+61871234567"),  # local format is read as Australian
        ("02 9876 5432 x123", "+61298765432"),
        ("02 9876 5432 ext 4", "+61298765432"),
        ("+61298765432#12", "+61298765432"),
        ("+44 7412 345678", "+447412345678"),
        ("+1 (248) 123-4567", "+12481234567"),
        ("+64 21 234 5678", "+64212345678"),
    ],
)
def test_canonical_forms(raw: str, expected: str) -> None:
    assert canonical_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "+266696687",  # Twilio: caller ID blocked
        "266696687",
        "anonymous",
        "Anonymous",
        "+anonymous",
        "restricted",
        "unavailable",
        "1300 123 456",  # not a subscriber number
        "13 11 14",
        "2125551234",  # ten digits, no country code: can't be placed
        "+0412345678",  # a plus with a trunk zero is not a country code
        "+61 1300 123 456",
        "12345",
        "+1234567890123456",  # too long for E.164
        "x123",
    ],
)
def test_unusable_numbers_are_none(raw: str | None) -> None:
    assert canonical_phone(raw) is None


def test_foreign_numbers_never_collide_with_australian_ones() -> None:
    # The old "last nine digits" comparison made these equal.
    assert canonical_phone("+447412345678") != canonical_phone("0412345678")
    assert canonical_phone("+64212345678") != canonical_phone("0212345678")
    assert canonical_phone("+12481234567") != canonical_phone("0481234567")
