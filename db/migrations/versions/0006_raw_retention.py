"""Retention for raw provider payloads: ops-worker may delete what it has finished with.

``retell_events_raw``, ``tool_requests_raw`` and ``quarantine_events`` hold verbatim caller
content (transcripts, messages) duplicated from the call records. They exist for replay and
investigation, not as the record of care, so they are deleted after a retention period (ADR 0004,
default 90 days). Only finished rows are ever deleted: an event or request still waiting for
replay or an operator's decision is kept until someone resolves it.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(
        """
        GRANT DELETE ON retell_events_raw, tool_requests_raw, quarantine_events TO app_ops;
        CREATE INDEX quarantine_events_received_idx ON quarantine_events (received_at);
        CREATE INDEX retell_events_raw_received_idx ON retell_events_raw (received_at);
        CREATE INDEX tool_requests_raw_received_idx ON tool_requests_raw (received_at);
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
