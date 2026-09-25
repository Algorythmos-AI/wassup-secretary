"""Clinic analytics: aggregates computed in SQL over one clinic's calls.

Dates are the clinic's own calendar: every call stores ``local_date`` / ``local_hour`` /
``local_dow`` in the clinic's timezone at write time, so "today" and "by hour" mean the same thing
to a clinic in Sydney and one in Brisbane (no daylight saving) without any timezone arithmetic at
query time. Responses are counts and totals only — no caller data — so they are not audited as
personal-data reads.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from wassup_core.db import clinic_scope

from core_api.schemas import AnalyticsSummary, UsageReport
from core_api.staff import Staff, current_staff

router = APIRouter(prefix="/v1")
StaffDep = Annotated[Staff, Depends(current_staff)]

DEFAULT_DAYS = 30
MAX_DAYS = 400
TOP_INTENTS = 10
WORKFLOW_STATES = ("pending", "following_up", "addressed", "no_action_needed")

# Fixed statements; every value is a bound parameter.
_TOTALS = text(
    """
    SELECT count(*) AS calls,
           round(avg(duration_seconds))::int AS avg_duration_seconds,
           coalesce(sum(duration_seconds), 0) AS total_duration_seconds,
           coalesce(sum(cost_usd), 0) AS cost_usd,
           count(*) FILTER (WHERE is_priority) AS priority,
           count(*) FILTER (WHERE is_reception_action) AS reception_action,
           count(*) FILTER (WHERE workflow_status = 'pending') AS pending,
           count(*) FILTER (WHERE workflow_status = 'following_up') AS following_up,
           count(*) FILTER (WHERE workflow_status = 'addressed') AS addressed,
           count(*) FILTER (WHERE workflow_status = 'no_action_needed') AS no_action_needed
    FROM calls WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date
    """
)
_BY_DAY = text(
    """
    SELECT local_date AS key, count(*) AS calls FROM calls
    WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date GROUP BY 1
    """
)
_BY_HOUR = text(
    """
    SELECT local_hour AS key, count(*) AS calls FROM calls
    WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date
      AND local_hour IS NOT NULL GROUP BY 1
    """
)
_BY_DOW = text(
    """
    SELECT local_dow AS key, count(*) AS calls FROM calls
    WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date
      AND local_dow IS NOT NULL GROUP BY 1
    """
)
_SENTIMENT = text(
    """
    SELECT coalesce(lower(sentiment), 'unknown') AS key, count(*) AS calls FROM calls
    WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date GROUP BY 1
    """
)
_INTENTS = text(
    """
    SELECT intent AS key, count(*) AS calls FROM calls
    WHERE clinic_id = :c AND local_date BETWEEN :from_date AND :to_date AND intent IS NOT NULL
    GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT :top
    """
)


def _range(timezone: str, from_date: date | None, to_date: date | None) -> tuple[date, date]:
    today = datetime.now(ZoneInfo(timezone)).date()
    end = to_date or today
    try:
        start = from_date or end - timedelta(days=DEFAULT_DAYS - 1)
    except OverflowError as exc:  # e.g. ?to=0001-01-05: before the calendar starts
        raise HTTPException(status_code=400, detail="Date out of range") from exc
    if start > end:
        raise HTTPException(status_code=400, detail="'from' must not be after 'to'")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Range is limited to {MAX_DAYS} days")
    return start, end


@router.get("/clinics/{clinic_id}/analytics/summary", response_model=AnalyticsSummary)
async def summary(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    staff.require(clinic_id)
    engine: AsyncEngine = request.app.state.engine
    async with clinic_scope(engine, [clinic_id]) as conn:
        timezone = (
            await conn.execute(text("SELECT timezone FROM clinics WHERE id = :c"), {"c": clinic_id})
        ).scalar_one()
        start, end = _range(timezone, from_date, to_date)
        params = {"c": clinic_id, "from_date": start, "to_date": end, "top": TOP_INTENTS}
        totals = (await conn.execute(_TOTALS, params)).mappings().one()
        by_day = {r.key: r.calls for r in await conn.execute(_BY_DAY, params)}
        by_hour = {r.key: r.calls for r in await conn.execute(_BY_HOUR, params)}
        by_dow = {r.key: r.calls for r in await conn.execute(_BY_DOW, params)}
        sentiment = {r.key: r.calls for r in await conn.execute(_SENTIMENT, params)}
        intents = [
            {"intent": r.key, "calls": r.calls} for r in await conn.execute(_INTENTS, params)
        ]

    days = (end - start).days + 1
    cost: Decimal = totals["cost_usd"]
    return {
        "clinic_id": str(clinic_id),
        "timezone": timezone,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "totals": {
            "calls": totals["calls"],
            "avg_duration_seconds": totals["avg_duration_seconds"],
            "total_duration_seconds": totals["total_duration_seconds"],
            "cost_usd": str(cost.quantize(Decimal("0.01"))),
            "priority": totals["priority"],
            "reception_action": totals["reception_action"],
        },
        "workflow": {state: totals[state] for state in WORKFLOW_STATES},
        # Every day, hour and weekday is present (zero-filled), so charts need no gap handling.
        "by_day": [
            {"date": day.isoformat(), "calls": by_day.get(day, 0)}
            for day in (start + timedelta(days=i) for i in range(days))
        ],
        "by_hour": [{"hour": h, "calls": by_hour.get(h, 0)} for h in range(24)],
        "by_weekday": [{"weekday": d, "calls": by_dow.get(d, 0)} for d in range(7)],  # Mon=0
        "sentiment": sentiment,
        "top_intents": intents,
    }


_USAGE = text(
    """
    SELECT day, calls, minutes, provider_cost_usd FROM usage_daily
    WHERE clinic_id = :c AND day BETWEEN :from_date AND :to_date ORDER BY day
    """
)


@router.get("/clinics/{clinic_id}/usage", response_model=UsageReport)
async def usage(
    clinic_id: uuid.UUID,
    staff: StaffDep,
    request: Request,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """Billing view (admins and owners): per-day calls, minutes and voice-provider cost, rolled
    up hourly by ops-worker. Days without calls are omitted."""
    staff.require(clinic_id, "admin")
    engine: AsyncEngine = request.app.state.engine
    async with clinic_scope(engine, [clinic_id]) as conn:
        timezone = (
            await conn.execute(text("SELECT timezone FROM clinics WHERE id = :c"), {"c": clinic_id})
        ).scalar_one()
        start, end = _range(timezone, from_date, to_date)
        rows = (
            await conn.execute(_USAGE, {"c": clinic_id, "from_date": start, "to_date": end})
        ).all()
    days = [
        {
            "date": r.day.isoformat(),
            "calls": r.calls,
            "minutes": str(r.minutes),
            "provider_cost_usd": str(r.provider_cost_usd),
        }
        for r in rows
    ]
    return {
        "clinic_id": str(clinic_id),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": days,
        "totals": {
            "calls": sum(r.calls for r in rows),
            "minutes": str(sum((r.minutes for r in rows), Decimal(0))),
            "provider_cost_usd": str(sum((r.provider_cost_usd for r in rows), Decimal(0))),
        },
    }
