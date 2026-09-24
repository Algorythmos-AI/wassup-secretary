"""Live dashboard events: core-api streams a clinic's outbox events, and emits workflow changes.

- core-api may read a clinic's outbox events (ids-only payloads, never personal data) to stream
  them to that clinic's signed-in staff (row-level security scopes it to the clinic, as always).
- core-api may add ``call.workflow`` events, so a status change made on one screen reaches every
  other screen of that clinic.
- ``(clinic_id, id)`` makes "events for this clinic after id N" an index range scan.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(
        """
        GRANT SELECT (id, clinic_id, event_type, payload, created_at, dedupe_key) ON outbox_events TO app_core;
        GRANT INSERT (clinic_id, event_type, dedupe_key, payload) ON outbox_events TO app_core;
        CREATE INDEX outbox_clinic_id_idx ON outbox_events (clinic_id, id);
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
