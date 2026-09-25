# ADR 0001 — Python and FastAPI for the new platform

**Status:** Accepted (owner decision, 2026-09-24)

## Context
The legacy dashboard is a Node service with a duplicated local backend and no type checking.
The rest of the WASSUP product family (scheduling, patients, transcription) is Python/FastAPI.

## Decision
Build WASSUP Secretary in Python 3.12 with FastAPI, SQLAlchemy 2 (async), Alembic, pydantic v2,
managed as a single uv workspace. mypy runs in strict mode on library and service code.

## Consequences
- One language across the WASSUP services; the team's templates and conventions apply.
- The legacy code is **not** translated line by line (see ADR 0007): behaviour is re-specified
  and re-tested here, and parity is proven against the legacy service from outside this repo.
- Voice-path latency must be measured (tool calls have a hard budget; see ADR 0004).
