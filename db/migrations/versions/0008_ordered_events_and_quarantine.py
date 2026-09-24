"""Gap-free live events, a narrower core-api insert, and quarantine that waits for a human.

1. **Live streams can't skip events.** Outbox ids are assigned at insert but become visible at
   commit, and commits land out of order: a stream that had delivered id N+1 would never deliver
   id N committed a moment later. Each row now records the writing transaction's id
   (``xact_id``). Streams deliver in (xact_id, id) order and only rows from transactions older
   than every transaction still running (the snapshot's xmin), so nothing can appear "behind" a
   stream's cursor later.
2. **core-api may only add ``call.workflow`` events.** Its INSERT grant (0007) would otherwise let
   it add any event type, including ``message.urgent``, which emails a clinic. A RESTRICTIVE policy
   narrows it without widening anything.
3. **Quarantined items are kept until resolved.** A quarantined webhook never becomes a call row, so
   its quarantine record is the only copy of that call. ``resolved_at`` marks an operator's
   decision; retention only deletes resolved records, and ops-worker alerts while any are open.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SQL = r"""
ALTER TABLE outbox_events
  ADD COLUMN xact_id bigint NOT NULL DEFAULT (pg_current_xact_id()::text::bigint);
CREATE INDEX outbox_clinic_xact_idx ON outbox_events (clinic_id, xact_id, id);
DROP INDEX outbox_clinic_id_idx;
GRANT SELECT (xact_id) ON outbox_events TO app_core;

CREATE POLICY outbox_core_inserts_workflow_only ON outbox_events AS RESTRICTIVE
  FOR INSERT TO app_core
  WITH CHECK (event_type = 'call.workflow' AND dedupe_key LIKE 'call.workflow:%');

ALTER TABLE quarantine_events ADD COLUMN resolved_at timestamptz;
ALTER TABLE quarantine_events ADD COLUMN resolution text;
CREATE INDEX quarantine_events_open_idx ON quarantine_events (received_at) WHERE resolved_at IS NULL;
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
