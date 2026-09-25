"""Nothing a caller says is lost: raw tool requests, replay bookkeeping, dead-letter handling.

- ``tool_requests_raw``: every signed tool request is stored (committed) *before* the tool runs,
  mirroring what ``retell_events_raw`` does for webhooks. If the tool then times out or the
  database stumbles, the request is still here, and ops-worker replays it through voice-gateway
  (the tool's dedupe key makes the replay exactly-once). Not under row-level security, like
  ``retell_events_raw``: it is written before the clinic is known, and only app_voice (write) and
  app_ops (replay) have any access. It holds caller-supplied content, so it is covered by the raw
  event retention policy.
- ``replay_attempts`` on both raw tables bounds how often ops-worker retries one item.
- ``outbox_events.status`` gains ``abandoned``: an operator's explicit decision about a dead
  letter (``dead`` keeps the outbox health check red until someone decides).
- The claim query reads pending *and* expired-lease rows; its index now covers both.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE tool_requests_raw (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  dedupe_key        text NOT NULL UNIQUE,
  tool              text NOT NULL,
  clinic_slug       text NOT NULL,
  provider_call_id  text NOT NULL,
  agent_id          text,
  clinic_id         uuid REFERENCES clinics(id),
  payload           jsonb NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,
  outcome           text,
  replay_attempts   integer NOT NULL DEFAULT 0,
  last_error        text
);
CREATE INDEX tool_requests_raw_open_idx ON tool_requests_raw (received_at) WHERE completed_at IS NULL;
GRANT SELECT, INSERT, UPDATE ON tool_requests_raw TO app_voice;
GRANT SELECT, UPDATE ON tool_requests_raw TO app_ops;

ALTER TABLE retell_events_raw ADD COLUMN replay_attempts integer NOT NULL DEFAULT 0;
CREATE INDEX retell_events_raw_open_idx ON retell_events_raw (received_at) WHERE processed_at IS NULL;

ALTER TABLE outbox_events DROP CONSTRAINT outbox_events_status_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_status_check
  CHECK (status IN ('pending', 'processing', 'done', 'dead', 'abandoned'));
DROP INDEX outbox_pending_idx;
CREATE INDEX outbox_due_idx ON outbox_events (available_at) WHERE status IN ('pending', 'processing');
CREATE INDEX outbox_dead_idx ON outbox_events (id) WHERE status = 'dead';
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
