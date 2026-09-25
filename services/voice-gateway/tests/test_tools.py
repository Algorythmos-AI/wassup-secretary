"""Voice tool calls against the migrated database, with the service connected as app_voice."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from voice_gateway.main import build_app
from voice_gateway.settings import VoiceGatewaySettings
from voice_gateway.signature import sign
from wassup_core.db import make_engine
from wassup_core.settings import Environment

from tests.support.database import Seed, as_role, line_check_running

pytestmark = pytest.mark.db

KEY = "test-signing-key"
AGENT_A = "agent_test_a"
NUMBER_A = "+61400000101"
AI_LINES = "+61400000900"


@pytest.fixture(scope="module")
def patients(db_engine: Engine, seed: Seed) -> dict[str, uuid.UUID]:
    """Synthetic patients for clinic A (and one for clinic B that A must never see)."""
    ids = {name: uuid.uuid4() for name in ("alive", "deceased", "other_clinic")}
    with db_engine.connect() as conn, conn.begin():
        as_role(conn, "wassup_owner", [seed.clinic_a, seed.clinic_b])
        rows = [
            (ids["alive"], seed.clinic_a, "pms-1", "Test", "Patient", "1980-01-15", False),
            (ids["deceased"], seed.clinic_a, "pms-2", "Late", "Patient", "1940-03-03", True),
            (ids["other_clinic"], seed.clinic_b, "pms-3", "Other", "Clinic", "1990-05-05", False),
        ]
        for pid, clinic, pms, first, last, dob, deceased in rows:
            conn.execute(
                text(
                    "INSERT INTO patients (id, clinic_id, source_pms_id, first_name, last_name, date_of_birth, is_deceased) "
                    "VALUES (:id, :c, :pms, :f, :l, :dob, :d) ON CONFLICT DO NOTHING"
                ),
                {
                    "id": pid,
                    "c": clinic,
                    "pms": pms,
                    "f": first,
                    "l": last,
                    "dob": dob,
                    "d": deceased,
                },
            )
    return ids


@pytest.fixture
async def voice_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=4)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_voice(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_voice")
        cursor.close()

    yield engine
    await engine.dispose()


def _client(engine: AsyncEngine, **overrides: Any) -> httpx.AsyncClient:
    settings = VoiceGatewaySettings(
        environment=Environment.TEST, retell_api_key=KEY, ai_line_numbers=AI_LINES, **overrides
    )
    transport = httpx.ASGITransport(app=build_app(settings, engine=engine))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _tool(
    client: httpx.AsyncClient,
    tool: str,
    args: dict[str, Any],
    *,
    call_id: str,
    slug: str = "test-clinic-a",
    **call: Any,
) -> httpx.Response:
    envelope = {
        "name": tool,
        "call": {
            "call_id": call_id,
            "agent_id": AGENT_A,
            "direction": "inbound",
            "from_number": "+61400000555",
            "to_number": NUMBER_A,
            **call,
        },
        "args": args,
    }
    body = json.dumps(envelope).encode()
    header = sign(body, KEY, int(time.time() * 1000))
    return await client.post(
        f"/v1/retell/tools/{slug}/{tool}", content=body, headers={"x-retell-signature": header}
    )


def _rows(db_engine: Engine, sql: str, **params: Any) -> list[Any]:
    with db_engine.connect() as conn:
        return list(conn.execute(text(sql), params).mappings())


async def test_capture_message_is_written_once_even_when_retried(
    voice_engine: AsyncEngine, db_engine: Engine, seed: Seed
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    args = {
        "category": "general",
        "detail": "Please call back about the test appointment.",
        "callback_number": "+61400000555",
    }
    async with _client(voice_engine) as client:
        first = await _tool(client, "capture_message", args, call_id=call_id)
        second = await _tool(client, "capture_message", args, call_id=call_id)
    assert first.json() == second.json() == {"ok": True}
    [msg] = _rows(db_engine, "SELECT * FROM messages WHERE provider_call_id = :c", c=call_id)
    assert msg["clinic_id"] == seed.clinic_a
    assert (
        len(
            _rows(
                db_engine, "SELECT id FROM tool_invocations WHERE provider_call_id = :c", c=call_id
            )
        )
        == 1
    )


async def test_concurrent_retries_still_write_once(
    voice_engine: AsyncEngine, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    args = {"category": "general", "detail": "Concurrent retry test."}
    async with _client(voice_engine) as client:
        responses = await asyncio.gather(
            *(_tool(client, "capture_message", args, call_id=call_id) for _ in range(3))
        )
    assert all(r.status_code == 200 for r in responses)
    assert (
        len(_rows(db_engine, "SELECT id FROM messages WHERE provider_call_id = :c", c=call_id)) == 1
    )


async def test_urgent_message_queues_an_alert_event(
    voice_engine: AsyncEngine, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        # The category is free text from the agent: urgency comes from the flag, not the word.
        await _tool(
            client,
            "capture_message",
            {"category": "post_op", "detail": "Test urgent message.", "urgent": True},
            call_id=call_id,
        )
        await _tool(
            client,
            "capture_message",
            {"category": "general", "detail": "Test routine message."},
            call_id=call_id,
        )
    stored = _rows(
        db_engine,
        "SELECT detail, urgent FROM messages WHERE provider_call_id = :c ORDER BY detail",
        c=call_id,
    )
    assert [(m["detail"], m["urgent"]) for m in stored] == [
        ("Test routine message.", False),
        ("Test urgent message.", True),
    ]
    events = _rows(
        db_engine,
        "SELECT event_type, payload FROM outbox_events WHERE payload->>'call' = :c "
        "ORDER BY event_type",
        c=call_id,
    )
    assert [(e["event_type"], e["payload"]) for e in events] == [
        ("message.captured", {"call": call_id}),
        ("message.urgent", {"call": call_id}),
    ]


async def test_create_promise_sets_due_time(voice_engine: AsyncEngine, db_engine: Engine) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "create_promise",
            {"promise_type": "callback", "due_in_hours": 24},
            call_id=call_id,
        )
    assert response.json()["ok"] is True
    [promise] = _rows(db_engine, "SELECT * FROM promises WHERE provider_call_id = :c", c=call_id)
    assert promise["status"] == "open"


async def test_url_slug_must_match_the_resolved_clinic(
    voice_engine: AsyncEngine, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "capture_message",
            {"detail": "Wrong clinic URL."},
            call_id=call_id,
            slug="test-clinic-b",
        )
    assert response.json() == {"ok": False, "degraded": True}  # never claims it was saved
    assert _rows(db_engine, "SELECT id FROM messages WHERE provider_call_id = :c", c=call_id) == []
    assert _rows(
        db_engine, "SELECT id FROM quarantine_events WHERE reason LIKE 'tool_clinic_slug_mismatch%'"
    )


async def test_lookup_exact_match_returns_opaque_ref_not_names(
    voice_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "lookup_patient",
            {"first_name": "test", "last_name": "PATIENT", "date_of_birth": "1980-01-15"},
            call_id=f"call_{uuid.uuid4().hex}",
        )
    body = response.json()
    assert body == {"matched": True, "patient_ref": str(patients["alive"]), "verified": False}
    assert "Test" not in response.text and "Patient" not in response.text


async def test_lookup_is_verified_only_from_the_number_on_file(
    voice_engine: AsyncEngine, db_engine: Engine, patients: dict[str, uuid.UUID]
) -> None:
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text("UPDATE patients SET phone = '+61400000777' WHERE id = :id"),
            {"id": patients["alive"]},
        )
    args = {"first_name": "Test", "last_name": "Patient", "date_of_birth": "1980-01-15"}
    async with _client(voice_engine) as client:
        # Ringing from the number on file (written the local way): verified.
        on_file = await _tool(
            client,
            "lookup_patient",
            args,
            call_id=f"call_{uuid.uuid4().hex}",
            from_number="0400 000 777",
        )
        # Ringing from another number: matched, not verified. The agent must not say specifics.
        other = await _tool(
            client,
            "lookup_patient",
            args,
            call_id=f"call_{uuid.uuid4().hex}",
            from_number="+61400000555",
        )
        # Caller ID withheld: no lookup at all.
        withheld = await _tool(
            client, "lookup_patient", args, call_id=f"call_{uuid.uuid4().hex}", from_number=None
        )
    assert on_file.json()["verified"] is True
    assert other.json() == {
        "matched": True,
        "patient_ref": str(patients["alive"]),
        "verified": False,
    }
    assert withheld.json() == {"matched": False, "reason": "caller_id_withheld"}


async def test_withheld_caller_id_markers_and_foreign_numbers_never_verify(
    voice_engine: AsyncEngine, db_engine: Engine, patients: dict[str, uuid.UUID]
) -> None:
    """The provider's blocked-ID sentinel (+266696687, nine digits) must count as withheld, and
    a foreign number sharing its last nine digits with the patient's must not verify."""
    with db_engine.connect() as conn, conn.begin():
        conn.execute(
            text("UPDATE patients SET phone = '02 6669 6687' WHERE id = :id"),  # +61266696687
            {"id": patients["alive"]},
        )
    args = {"first_name": "Test", "last_name": "Patient", "date_of_birth": "1980-01-15"}
    async with _client(voice_engine) as client:
        answers = {
            label: (
                await _tool(
                    client,
                    "lookup_patient",
                    args,
                    call_id=f"call_{uuid.uuid4().hex}",
                    from_number=number,
                )
            ).json()
            for label, number in {
                "twilio_anonymous": "+266696687",
                "word": "anonymous",
                "restricted": "Restricted",
                "uk_same_tail": "+44266696687",
                "own_line_intl": "+61 2 6669 6687",
                "own_line_local": "0266696687",
            }.items()
        }
    for label in ("twilio_anonymous", "word", "restricted"):
        assert answers[label] == {"matched": False, "reason": "caller_id_withheld"}, label
    assert answers["uk_same_tail"]["matched"] and answers["uk_same_tail"]["verified"] is False
    assert answers["own_line_intl"]["verified"] is True
    assert answers["own_line_local"]["verified"] is True


@pytest.fixture
async def wide_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url, pool_size=12)

    @event.listens_for(engine.sync_engine, "connect")
    def _as_app_voice(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("SET ROLE app_voice")
        cursor.close()

    yield engine
    await engine.dispose()


async def test_per_caller_cap_holds_under_concurrent_calls(
    wide_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    """Twelve simultaneous lookups from one number: exactly five get through (the count and
    the claim are serialised per caller, not just per call)."""
    args = {"first_name": "Nobody", "last_name": "Here", "date_of_birth": "1999-09-09"}
    async with _client(wide_engine) as client:
        answers = await asyncio.gather(
            *[
                _tool(
                    client,
                    "lookup_patient",
                    args,
                    call_id=f"call_{uuid.uuid4().hex}",
                    from_number="+61400000321",
                )
                for _ in range(12)
            ]
        )
    reasons = [a.json().get("reason") for a in answers]
    assert reasons.count(None) == 5 and reasons.count("limit_reached") == 7


async def test_lookups_are_capped_per_caller_number_across_calls(
    voice_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    args = {"first_name": "Nobody", "last_name": "Here", "date_of_birth": "1999-09-09"}
    number = "+61400000123"
    async with _client(voice_engine) as client:
        answers = [
            (
                await _tool(
                    client,
                    "lookup_patient",
                    args,
                    call_id=f"call_{uuid.uuid4().hex}",
                    from_number=number,
                )
            ).json()
            for _ in range(6)
        ]
        # A different number is unaffected.
        fresh = (
            await _tool(
                client,
                "lookup_patient",
                args,
                call_id=f"call_{uuid.uuid4().hex}",
                from_number="+61400000124",
            )
        ).json()
    assert [a.get("reason") for a in answers] == [None] * 5 + ["limit_reached"]
    assert fresh == {"matched": False}


async def test_lookup_deceased_is_indistinguishable_from_no_match(
    voice_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    async with _client(voice_engine) as client:
        deceased = await _tool(
            client,
            "lookup_patient",
            {"first_name": "Late", "last_name": "Patient", "date_of_birth": "1940-03-03"},
            call_id=f"call_{uuid.uuid4().hex}",
        )
        nobody = await _tool(
            client,
            "lookup_patient",
            {"first_name": "No", "last_name": "Body", "date_of_birth": "1970-01-01"},
            call_id=f"call_{uuid.uuid4().hex}",
        )
    assert deceased.json() == nobody.json() == {"matched": False}


async def test_lookup_cannot_see_another_clinics_patient(
    voice_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "lookup_patient",
            {"first_name": "Other", "last_name": "Clinic", "date_of_birth": "1990-05-05"},
            call_id=f"call_{uuid.uuid4().hex}",
        )
    assert response.json() == {"matched": False}


async def test_lookup_is_capped_per_call(
    voice_engine: AsyncEngine, patients: dict[str, uuid.UUID]
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        for dob in ("1970-01-01", "1970-01-02"):
            await _tool(
                client,
                "lookup_patient",
                {"first_name": "No", "last_name": "Body", "date_of_birth": dob},
                call_id=call_id,
            )
        third = await _tool(
            client,
            "lookup_patient",
            {"first_name": "Test", "last_name": "Patient", "date_of_birth": "1980-01-15"},
            call_id=call_id,
        )
    assert third.json() == {"matched": False, "reason": "limit_reached"}


async def test_invalid_arguments_are_reported_not_written(voice_engine: AsyncEngine) -> None:
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "create_promise",
            {"promise_type": "x", "due_in_hours": -5},
            call_id=f"call_{uuid.uuid4().hex}",
        )
    assert response.json() == {"ok": False, "error": "invalid_arguments"}


async def test_budget_exceeded_returns_fallback(voice_engine: AsyncEngine) -> None:
    async with _client(voice_engine, tool_budget_ms=1) as client:
        response = await _tool(
            client,
            "lookup_patient",
            {"first_name": "Test", "last_name": "Patient", "date_of_birth": "1980-01-15"},
            call_id=f"call_{uuid.uuid4().hex}",
        )
    assert response.json() == {"matched": False, "degraded": True}


async def test_synthetic_call_touches_nothing(voice_engine: AsyncEngine, db_engine: Engine) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    line_check_running(db_engine, "+61400000900", NUMBER_A)
    async with _client(voice_engine) as client:
        response = await _tool(
            client,
            "capture_message",
            {"detail": "line check"},
            call_id=call_id,
            from_number="+61400000900",
        )
    assert response.json() == {"ok": True, "synthetic": True}
    for table in ("tool_invocations", "tool_requests_raw"):
        assert (
            _rows(db_engine, f"SELECT id FROM {table} WHERE provider_call_id = :c", c=call_id)  # noqa: S608
            == []
        )


async def test_unknown_tool_and_bad_signature(voice_engine: AsyncEngine) -> None:
    async with _client(voice_engine) as client:
        assert (await _tool(client, "delete_everything", {}, call_id="call_x")).status_code == 404
        body = b'{"call":{"call_id":"c"},"args":{}}'
        bad = await client.post(
            "/v1/retell/tools/test-clinic-a/capture_message",
            content=body,
            headers={"x-retell-signature": sign(body, "wrong", int(time.time() * 1000))},
        )
        assert bad.status_code == 401


async def test_write_requests_are_kept_raw_and_lookups_are_not(
    voice_engine: AsyncEngine, db_engine: Engine, patients: dict[str, uuid.UUID]
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        await _tool(client, "capture_message", {"detail": "Please call back."}, call_id=call_id)
        await _tool(
            client,
            "lookup_patient",
            {"first_name": "Test", "last_name": "Patient", "date_of_birth": "1980-01-15"},
            call_id=call_id,
        )
    rows = _rows(
        db_engine,
        "SELECT tool, outcome, completed_at FROM tool_requests_raw WHERE provider_call_id = :c",
        c=call_id,
    )
    assert [(r["tool"], r["outcome"]) for r in rows] == [("capture_message", "done")]
    assert rows[0]["completed_at"] is not None


async def test_nul_characters_never_break_a_write(
    voice_engine: AsyncEngine, db_engine: Engine
) -> None:
    call_id = f"call_{uuid.uuid4().hex}"
    async with _client(voice_engine) as client:
        response = await _tool(
            client, "capture_message", {"detail": "Call me\u0000 back"}, call_id=call_id
        )
    assert response.json() == {"ok": True}
    [message] = _rows(
        db_engine, "SELECT detail FROM messages WHERE provider_call_id = :c", c=call_id
    )
    assert message["detail"] == "Call me back"
