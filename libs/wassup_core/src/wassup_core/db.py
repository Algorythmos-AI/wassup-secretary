"""Tenancy-aware database access.

Every service connects as its own role (``app_voice`` / ``app_core`` / ``app_ops``), which is
subject to row-level security. Tenant data is only reachable inside :func:`clinic_scope`, which
opens a transaction and declares the clinic ids that transaction may touch. Outside a scope,
tenant tables return zero rows — isolation fails closed.

The clinic list is always passed as a bound parameter to ``set_config(..., true)`` (transaction-
local), never formatted into SQL, and is validated as UUIDs first.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_SET_CLINICS = text("SELECT set_config('app.clinic_ids', :clinics, true)")


def async_url(url: str) -> str:
    """Normalise a Postgres URL to the asyncpg driver (platforms hand out plain postgres://)."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def make_engine(
    url: str,
    *,
    pool_size: int = 5,
    pool_timeout_s: float = 5.0,
    statement_timeout_ms: int | None = None,
) -> AsyncEngine:
    """Create an async engine. ``pool_timeout_s`` bounds how long a request waits for a
    connection — the voice path uses a very short one so a saturated pool degrades to a
    fallback answer instead of dead air."""
    connect_args: dict[str, object] = {}
    if statement_timeout_ms is not None:
        connect_args["server_settings"] = {"statement_timeout": str(statement_timeout_ms)}
    return create_async_engine(
        async_url(url),
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=pool_timeout_s,
        pool_pre_ping=True,
        # Bound values are personal data (phone numbers, names, notes); keep them out of the
        # exception messages SQLAlchemy builds, which end up in error reports.
        hide_parameters=True,
        connect_args=connect_args,
    )


def clinic_array_literal(clinic_ids: Iterable[uuid.UUID | str]) -> str:
    """Validate ids as UUIDs and render the Postgres array literal for ``set_config``."""
    ids = [str(c if isinstance(c, uuid.UUID) else uuid.UUID(str(c))) for c in clinic_ids]
    return "{" + ",".join(ids) + "}"


@asynccontextmanager
async def clinic_scope(
    engine: AsyncEngine, clinic_ids: Iterable[uuid.UUID | str]
) -> AsyncIterator[AsyncConnection]:
    """A transaction limited to ``clinic_ids``. Commits on success, rolls back on error."""
    literal = clinic_array_literal(clinic_ids)
    async with engine.begin() as conn:
        await conn.execute(_SET_CLINICS, {"clinics": literal})
        yield conn


@asynccontextmanager
async def unscoped(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A transaction with no clinic context: only non-tenant tables and the SECURITY DEFINER
    resolvers are usable. Use it to resolve which clinic a request belongs to."""
    async with engine.begin() as conn:
        yield conn
