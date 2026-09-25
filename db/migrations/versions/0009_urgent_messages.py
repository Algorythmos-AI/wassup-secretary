"""Urgent messages stay urgent at rest.

``capture_message`` receives ``urgent`` from the voice agent but used it only to pick the outbox
event (``message.urgent`` alerts the clinic); the message row never recorded it, so the dashboard
could only guess urgency from the free-text category. ``messages.urgent`` now stores it.

Rows written before this migration are backfilled: urgent when their ``message.urgent`` outbox
event exists (same dedupe key) or their category is "urgent". Row-level security is forced for
the owner as well, so the backfill lifts it for these two tables inside this migration's
transaction only and restores it before commit (the tenancy suite checks it is on afterwards).

A partial index answers "did this call leave an urgent message?" for the call list.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SQL = r"""
ALTER TABLE messages ADD COLUMN urgent boolean NOT NULL DEFAULT false;
CREATE INDEX messages_urgent_idx ON messages (clinic_id, provider_call_id) WHERE urgent;

ALTER TABLE messages NO FORCE ROW LEVEL SECURITY;
ALTER TABLE outbox_events NO FORCE ROW LEVEL SECURITY;
UPDATE messages m SET urgent = true
WHERE lower(m.category) = 'urgent'
   OR EXISTS (
     SELECT 1 FROM outbox_events o
     WHERE o.dedupe_key = 'message.urgent:' || m.dedupe_key AND o.clinic_id = m.clinic_id
   );
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY;
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
