"""A call's local date, hour and weekday across the Sydney clock change and in Brisbane.

Sydney moves from AEST (+10) to AEDT (+11) at 02:00 local on Sunday 4 October 2026; Brisbane
stays on +10 all year. Analytics group calls by these stored fields, so an off-by-one-hour here
would put calls in the wrong hour (or, near midnight, the wrong day) on every dashboard.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from voice_gateway.retell import to_record


def _local(started: datetime, zone: str) -> tuple[date | None, int | None, int | None]:
    call = {"call_id": "c", "start_timestamp": int(started.timestamp() * 1000)}
    return to_record("call_analyzed", call).local_fields(zone)


@pytest.mark.parametrize(
    ("utc", "zone", "expected"),
    [
        # 15:59 UTC Sat 3 Oct = 01:59 AEST Sun 4 Oct, the last minute before the change.
        (datetime(2026, 10, 3, 15, 59, tzinfo=UTC), "Australia/Sydney", (date(2026, 10, 4), 1, 6)),
        # One minute later the clocks jump: 16:00 UTC = 03:00 AEDT (02:xx never exists).
        (datetime(2026, 10, 3, 16, 0, tzinfo=UTC), "Australia/Sydney", (date(2026, 10, 4), 3, 6)),
        # Near midnight after the change: 13:30 UTC Sun = 00:30 AEDT Mon 5 Oct (+11, not +10).
        (datetime(2026, 10, 4, 13, 30, tzinfo=UTC), "Australia/Sydney", (date(2026, 10, 5), 0, 0)),
        # Brisbane never changes: the same instants are one hour behind Sydney after the change.
        (datetime(2026, 10, 3, 16, 0, tzinfo=UTC), "Australia/Brisbane", (date(2026, 10, 4), 2, 6)),
        (
            datetime(2026, 10, 4, 13, 30, tzinfo=UTC),
            "Australia/Brisbane",
            (date(2026, 10, 4), 23, 6),
        ),
        # Sydney's change back (5 Apr 2026, 03:00 AEDT -> 02:00 AEST): 16:30 UTC = 02:30 AEST.
        (datetime(2026, 4, 4, 16, 30, tzinfo=UTC), "Australia/Sydney", (date(2026, 4, 5), 2, 6)),
    ],
)
def test_local_fields_follow_the_clinic_clock(
    utc: datetime, zone: str, expected: tuple[date, int, int]
) -> None:
    assert _local(utc, zone) == expected


def test_a_call_without_a_start_has_no_local_fields() -> None:
    assert to_record("call_analyzed", {"call_id": "c"}).local_fields("Australia/Sydney") == (
        None,
        None,
        None,
    )
