"""Voice tool calls: POST /v1/retell/tools/{clinic_slug}/{tool}.

Guarantees (ADR 0004):
- Signed like the webhook; synthetic line-check calls are answered without touching data.
- The clinic comes from the signed agent AND the dialled number, and must match the clinic slug
  in the URL; any disagreement is quarantined and answered with the tool's safe fallback.
- Exactly-once: the invocation is claimed in ``tool_invocations`` (unique dedupe key) inside the
  same transaction as the tool's writes, so a retry — even a concurrent one — returns the stored
  result instead of writing twice.
- A hard time budget: on timeout or database trouble the caller hears the tool's fallback line
  instead of dead air.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from wassup_core.db import clinic_scope, unscoped
from wassup_core.http import problem
from wassup_core.logging import get_logger

from voice_gateway import store
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import verify
from voice_gateway.webhook import is_synthetic

router = APIRouter()
log = get_logger(__name__)

MAX_LOOKUPS_PER_CALL = 2


@dataclass(frozen=True)
class ToolContext:
    conn: AsyncConnection
    clinic_id: uuid.UUID
    call_id: str
    dedupe_key: str


class CaptureMessageArgs(BaseModel):
    category: str = Field(default="general", min_length=1, max_length=50)
    detail: str = Field(min_length=1, max_length=4000)
    callback_number: str | None = Field(default=None, max_length=32)
    urgent: bool = False


class CreatePromiseArgs(BaseModel):
    promise_type: str = Field(min_length=1, max_length=50)
    due_in_hours: float = Field(gt=0, le=720)


class LookupPatientArgs(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date


async def capture_message(ctx: ToolContext, args: CaptureMessageArgs) -> dict[str, Any]:
    await ctx.conn.execute(
        text(
            "INSERT INTO messages (clinic_id, provider_call_id, category, detail, callback_number, dedupe_key) "
            "VALUES (:clinic_id, :call_id, :category, :detail, :callback, :dedupe)"
        ),
        {
            "clinic_id": ctx.clinic_id,
            "call_id": ctx.call_id,
            "category": args.category,
            "detail": args.detail,
            "callback": args.callback_number,
            "dedupe": ctx.dedupe_key,
        },
    )
    event_type = "message.urgent" if args.urgent else "message.captured"
    await store.enqueue(
        ctx.conn, ctx.clinic_id, event_type, f"{event_type}:{ctx.dedupe_key}", {"call": ctx.call_id}
    )
    return {"ok": True}


async def create_promise(ctx: ToolContext, args: CreatePromiseArgs) -> dict[str, Any]:
    due_at = datetime.now(UTC) + timedelta(hours=args.due_in_hours)
    await ctx.conn.execute(
        text(
            "INSERT INTO promises (clinic_id, provider_call_id, promise_type, due_at, dedupe_key) "
            "VALUES (:clinic_id, :call_id, :type, :due_at, :dedupe)"
        ),
        {
            "clinic_id": ctx.clinic_id,
            "call_id": ctx.call_id,
            "type": args.promise_type,
            "due_at": due_at,
            "dedupe": ctx.dedupe_key,
        },
    )
    return {"ok": True, "due_at": due_at.isoformat()}


async def lookup_patient(ctx: ToolContext, args: LookupPatientArgs) -> dict[str, Any]:
    """Privacy-first lookup: exact match only; never returns names; a deceased patient is
    indistinguishable from no match; at most MAX_LOOKUPS_PER_CALL per call."""
    prior = await ctx.conn.execute(
        text(
            "SELECT count(*) FROM tool_invocations "
            "WHERE provider_call_id = :call_id AND tool = 'lookup_patient' AND dedupe_key <> :dedupe"
        ),
        {"call_id": ctx.call_id, "dedupe": ctx.dedupe_key},
    )
    if int(prior.scalar_one()) >= MAX_LOOKUPS_PER_CALL:
        return {"matched": False, "reason": "limit_reached"}
    rows = (
        await ctx.conn.execute(
            text(
                """
                SELECT id, is_deceased FROM patients
                WHERE clinic_id = :clinic_id AND date_of_birth = :dob
                  AND lower(last_name) = lower(:last) AND lower(first_name) = lower(:first)
                LIMIT 2
                """
            ),
            {
                "clinic_id": ctx.clinic_id,
                "dob": args.date_of_birth,
                "last": args.last_name.strip(),
                "first": args.first_name.strip(),
            },
        )
    ).all()
    if len(rows) != 1 or rows[0].is_deceased:
        return {"matched": False}
    return {"matched": True, "patient_ref": str(rows[0].id)}


@dataclass(frozen=True)
class ToolSpec:
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]
    fallback: dict[str, Any]


TOOLS: dict[str, ToolSpec] = {
    "capture_message": ToolSpec(
        CaptureMessageArgs, capture_message, {"ok": True, "degraded": True}
    ),
    "create_promise": ToolSpec(CreatePromiseArgs, create_promise, {"ok": True, "degraded": True}),
    "lookup_patient": ToolSpec(
        LookupPatientArgs, lookup_patient, {"matched": False, "degraded": True}
    ),
}


def dedupe_key(call_id: str, tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return "tool:" + hashlib.sha256(f"{call_id}|{tool}|{canonical}".encode()).hexdigest()


async def _invoke(
    engine: AsyncEngine,
    spec: ToolSpec,
    *,
    tool: str,
    clinic_slug: str,
    call: dict[str, Any],
    raw_args: dict[str, Any],
    parsed_args: BaseModel,
    payload: dict[str, Any],
) -> dict[str, Any]:
    call_id = str(call["call_id"])
    agent_id = call.get("agent_id") if isinstance(call.get("agent_id"), str) else None
    to_number = call.get("to_number") if isinstance(call.get("to_number"), str) else None

    async with unscoped(engine) as conn:
        clinic_id = await store.resolve_clinic(conn, agent_id, to_number)
        if clinic_id is None:
            await store.quarantine(conn, f"tool_unknown_agent_or_number:{tool}", agent_id, payload)
            log.error(
                "tool_quarantined", tool=tool, call_id=call_id, reason="unknown_agent_or_number"
            )
            return spec.fallback

    key = dedupe_key(call_id, tool, raw_args)
    started = time.perf_counter()
    async with clinic_scope(engine, [clinic_id]) as conn:
        slug = (
            await conn.execute(text("SELECT slug FROM clinics WHERE id = :id"), {"id": clinic_id})
        ).scalar()
        if slug != clinic_slug:
            log.error("tool_quarantined", tool=tool, call_id=call_id, reason="clinic_slug_mismatch")
            async with unscoped(engine) as qconn:
                await store.quarantine(
                    qconn, f"tool_clinic_slug_mismatch:{tool}", agent_id, payload
                )
            return spec.fallback
        claimed = await conn.execute(
            text(
                """
                INSERT INTO tool_invocations (clinic_id, provider_call_id, tool, dedupe_key, args_hash)
                VALUES (:clinic_id, :call_id, :tool, :key, :key)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """
            ),
            {"clinic_id": clinic_id, "call_id": call_id, "tool": tool, "key": key},
        )
        invocation_id = claimed.scalar()
        if invocation_id is None:  # a retry: return what the first attempt answered
            stored = await conn.execute(
                text("SELECT result FROM tool_invocations WHERE dedupe_key = :key"), {"key": key}
            )
            result = stored.scalar()
            log.info("tool_replayed", tool=tool, call_id=call_id)
            return dict(result) if isinstance(result, dict) else spec.fallback
        result = await spec.handler(ToolContext(conn, clinic_id, call_id, key), parsed_args)
        await conn.execute(
            text(
                "UPDATE tool_invocations SET result = CAST(:result AS jsonb), latency_ms = :ms WHERE id = :id"
            ),
            {
                "result": json.dumps(result),
                "ms": round((time.perf_counter() - started) * 1000),
                "id": invocation_id,
            },
        )
    log.info("tool_completed", tool=tool, call_id=call_id, clinic_id=str(clinic_id))
    return result


@router.post("/v1/retell/tools/{clinic_slug}/{tool}")
async def retell_tool(request: Request, clinic_slug: str, tool: str) -> JSONResponse:
    settings: VoiceGatewaySettings = request.app.state.settings
    engine: AsyncEngine = request.app.state.engine
    raw = await request.body()
    if not verify(raw, request.headers.get("x-retell-signature"), settings.retell_keys):
        return problem(401, "Invalid signature", "invalid_signature")
    spec = TOOLS.get(tool)
    if spec is None:
        return problem(404, "Unknown tool", "unknown_tool")
    try:
        payload: Any = json.loads(raw)
        call = payload["call"]
        raw_args = payload.get("args") or {}
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("call_id"), str)
            or not isinstance(raw_args, dict)
        ):
            raise ValueError("bad envelope")
    except (ValueError, KeyError, TypeError):
        return problem(400, "Invalid tool call", "invalid_tool_call")

    if is_synthetic(call, settings.ai_lines):
        return JSONResponse({"ok": True, "synthetic": True})
    try:
        parsed_args = spec.args_model.model_validate(raw_args)
    except ValidationError:
        return JSONResponse({"ok": False, "error": "invalid_arguments"})

    try:
        async with asyncio.timeout(settings.tool_budget_ms / 1000):
            result = await _invoke(
                engine,
                spec,
                tool=tool,
                clinic_slug=clinic_slug,
                call=call,
                raw_args=raw_args,
                parsed_args=parsed_args,
                payload=payload,
            )
    except TimeoutError:
        log.error("tool_degraded", tool=tool, call_id=call["call_id"], reason="timeout")
        result = spec.fallback
    except Exception as exc:
        log.error("tool_degraded", tool=tool, call_id=call["call_id"], reason=type(exc).__name__)
        result = spec.fallback
    return JSONResponse(result)
