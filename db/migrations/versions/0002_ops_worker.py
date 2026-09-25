"""ops-worker support: daily line-check runs and read access for alert delivery.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(
        r"""
        -- One row per AI line per local day. The UNIQUE claim makes the daily check idempotent
        -- across restarts and replicas. Platform data (no clinic_id): lines are public numbers.
        CREATE TABLE canary_runs (
          id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          run_date          date NOT NULL,
          to_number         text NOT NULL,
          from_number       text NOT NULL,
          status            text NOT NULL CHECK (status IN ('placing','placed','received','late','failed')),
          placed_at         timestamptz,
          provider_call_id  text,
          received_at       timestamptz,
          error             text,
          created_at        timestamptz NOT NULL DEFAULT now(),
          UNIQUE (run_date, to_number)
        );
        GRANT SELECT, INSERT, UPDATE ON canary_runs TO app_ops;
        GRANT SELECT, UPDATE ON canary_runs TO app_voice;

        -- Alert delivery reads the message it is delivering (clinic-scoped by RLS as always) and
        -- moves each delivery from 'sending' to 'sent'/'failed'.
        GRANT SELECT ON messages TO app_ops;
        GRANT UPDATE (status, sent_at) ON notifications_sent TO app_ops;
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
