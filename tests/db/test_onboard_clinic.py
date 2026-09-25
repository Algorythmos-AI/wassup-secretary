"""db/onboard_clinic.py: a new clinic and its first owner, without SQL."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.support.database import ROOT

pytestmark = pytest.mark.db


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "onboard_clinic", ROOT / "db" / "onboard_clinic.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()


def _url(db_engine: Engine) -> str:
    return db_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _env(**over: str) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    base = {
        "WASSUP_ENVIRONMENT": "test",
        "WASSUP_CLINIC_SLUG": f"clinic-{suffix}",
        "WASSUP_CLINIC_NAME": "Synthetic Onboarding Clinic",
        "WASSUP_CLINIC_STATE": "QLD",
        "WASSUP_CLINIC_TIMEZONE": "Australia/Brisbane",
        "WASSUP_CLINIC_OWNER_UID": f"uid{suffix}",
        "WASSUP_CLINIC_OWNER_EMAIL": f"owner-{suffix}@example.test",
        "WASSUP_CLINIC_AGENT_IDS": f"agent_onb_{suffix}",
        "WASSUP_CLINIC_NUMBERS": f"0400 {suffix[:3].translate(str.maketrans('abcdef', '123456'))} 000",
        "WASSUP_CLINIC_ALERT_EMAILS": "alerts@example.test",
    }
    return {**base, **over}


def _one(db_engine: Engine, sql: str, **params: Any) -> Any:
    with db_engine.connect() as conn:
        return conn.execute(text(sql), params).first()


def test_dry_run_checks_everything_and_writes_nothing(db_engine: Engine) -> None:
    env = _env()
    result = tool.run(_url(db_engine), env, apply=False)
    assert (
        result["created"] is True and result["agents_added"] == 1 and result["numbers_added"] == 1
    )
    assert (
        _one(db_engine, "SELECT 1 FROM clinics WHERE slug = :s", s=env["WASSUP_CLINIC_SLUG"])
        is None
    )
    assert (
        _one(
            db_engine,
            "SELECT 1 FROM staff_users WHERE firebase_uid = :u",
            u=env["WASSUP_CLINIC_OWNER_UID"],
        )
        is None
    )


def test_apply_makes_a_clinic_the_owner_can_sign_in_to_and_calls_can_reach(
    db_engine: Engine,
) -> None:
    env = _env()
    result = tool.run(_url(db_engine), env, apply=True)
    clinic_id = result["clinic_id"]
    clinic = _one(
        db_engine,
        "SELECT name, state, timezone, status, alert_contacts FROM clinics WHERE id = :c",
        c=clinic_id,
    )
    assert (clinic.state, clinic.timezone, clinic.status) == (
        "QLD",
        "Australia/Brisbane",
        "onboarding",
    )
    assert clinic.alert_contacts == ["alerts@example.test"]
    # What core-api does at sign-in: the resolver finds the owner membership.
    members = _one(
        db_engine,
        "SELECT clinic_id, role FROM staff_memberships(:u)",
        u=env["WASSUP_CLINIC_OWNER_UID"],
    )
    assert (str(members.clinic_id), members.role) == (clinic_id, "owner")
    # What voice-gateway does on a call: the agent and dialled number resolve to the clinic.
    number = "+61" + env["WASSUP_CLINIC_NUMBERS"].replace(" ", "")[1:]
    resolved = _one(
        db_engine,
        "SELECT resolve_clinic_for_call(:a, :n) AS c",
        a=env["WASSUP_CLINIC_AGENT_IDS"],
        n=number,
    )
    assert str(resolved.c) == clinic_id
    agent_env = _one(
        db_engine,
        "SELECT environment FROM clinic_voice_agents WHERE agent_id = :a",
        a=env["WASSUP_CLINIC_AGENT_IDS"],
    )
    assert agent_env.environment == "staging"  # a test environment maps agents as staging
    audited = _one(
        db_engine,
        "SELECT count(*) AS n FROM audit_log WHERE action = 'clinic.onboarded' AND clinic_id = :c",
        c=clinic_id,
    )
    assert audited.n == 1


def test_rerun_completes_a_clinic_and_refuses_to_change_it(db_engine: Engine) -> None:
    env = _env()
    url = _url(db_engine)
    first = tool.run(url, env, apply=True)
    again = tool.run(url, env, apply=True)
    assert again["clinic_id"] == first["clinic_id"] and again["created"] is False
    assert (again["agents_added"], again["numbers_added"]) == (0, 0)
    more = tool.run(
        url,
        {
            **env,
            "WASSUP_CLINIC_AGENT_IDS": env["WASSUP_CLINIC_AGENT_IDS"]
            + ",agent_second_"
            + uuid.uuid4().hex[:6],
        },
        apply=True,
    )
    assert more["agents_added"] == 1
    with pytest.raises(tool.OnboardRefused, match="different name"):
        tool.run(url, {**env, "WASSUP_CLINIC_NAME": "Renamed"}, apply=True)


def test_an_agent_or_number_of_another_clinic_refuses_the_run(db_engine: Engine) -> None:
    url = _url(db_engine)
    first = _env()
    tool.run(url, first, apply=True)
    stealing_agent = _env(WASSUP_CLINIC_AGENT_IDS=first["WASSUP_CLINIC_AGENT_IDS"])
    with pytest.raises(tool.OnboardRefused, match="another clinic"):
        tool.run(url, stealing_agent, apply=True)
    assert (
        _one(
            db_engine,
            "SELECT 1 FROM clinics WHERE slug = :s",
            s=stealing_agent["WASSUP_CLINIC_SLUG"],
        )
        is None
    )
    stealing_number = _env(WASSUP_CLINIC_NUMBERS=first["WASSUP_CLINIC_NUMBERS"])
    with pytest.raises(tool.OnboardRefused, match="another clinic"):
        tool.run(url, stealing_number, apply=True)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("WASSUP_CLINIC_SLUG", "Bad Slug", "SLUG"),
        ("WASSUP_CLINIC_STATE", "XX", "STATE"),
        ("WASSUP_CLINIC_TIMEZONE", "Europe/London", "Australia"),
        ("WASSUP_CLINIC_TIMEZONE", "Australia/Nowhere", "unknown timezone"),
        ("WASSUP_CLINIC_OWNER_UID", "a b", "OWNER_UID"),
        ("WASSUP_CLINIC_OWNER_EMAIL", "not-an-email", "OWNER_EMAIL"),
        ("WASSUP_CLINIC_NUMBERS", "+266696687", "not a usable number"),
        ("WASSUP_CLINIC_AGENT_IDS", "agent id with spaces", "AGENT_IDS"),
    ],
)
def test_bad_variables_are_refused_before_the_database(key: str, value: str, match: str) -> None:
    with pytest.raises(tool.OnboardRefused, match=match):
        tool.plan(_env(**{key: value}))


def test_production_apply_needs_the_slug_as_acknowledgement(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WASSUP_ADMIN_DATABASE_URL", _url(db_engine))
    monkeypatch.setenv("WASSUP_CLINIC_APPLY", "true")
    monkeypatch.delenv("WASSUP_ENVIRONMENT")
    assert tool.main() == 2
    assert (
        _one(db_engine, "SELECT 1 FROM clinics WHERE slug = :s", s=env["WASSUP_CLINIC_SLUG"])
        is None
    )
    monkeypatch.setenv("WASSUP_PRODUCTION_ACK", env["WASSUP_CLINIC_SLUG"])
    assert tool.main() == 0
    out = capsys.readouterr().out
    assert "committed" in out and env["WASSUP_CLINIC_OWNER_EMAIL"] not in out
    agent_env = _one(
        db_engine,
        "SELECT environment FROM clinic_voice_agents WHERE agent_id = :a",
        a=env["WASSUP_CLINIC_AGENT_IDS"],
    )
    assert agent_env.environment == "production"
