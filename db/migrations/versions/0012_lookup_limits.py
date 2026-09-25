"""Patient lookup limits: remember who asked.

``lookup_patient`` was limited per call (2). A caller can start many calls, so the limit must
also apply per **caller number** across calls: each lookup invocation now records a keyed hash
of the caller's number (never the number: nothing in this table can be turned back into one),
and the lookup counts recent lookups with the same key. Lookups with a withheld caller ID are
refused outright (there is nothing to count against, and nothing to verify with).

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

SQL = r"""
ALTER TABLE tool_invocations ADD COLUMN caller_key text CHECK (caller_key ~ '^[0-9a-f]{64}$');
CREATE INDEX tool_invocations_lookup_caller_idx
  ON tool_invocations (clinic_id, caller_key, created_at)
  WHERE tool = 'lookup_patient';
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
