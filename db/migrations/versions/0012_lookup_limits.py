"""Patient lookup limits: remember who asked.

``lookup_patient`` was limited per call (2). A caller can start many calls, so the limit must
also apply per **caller number** across calls: each tool invocation now records the caller's
number, and the lookup counts recent lookups from the same number. Lookups with a withheld
caller ID are refused outright (there is nothing to count against, and nothing to verify with).

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
ALTER TABLE tool_invocations ADD COLUMN caller_number text CHECK (char_length(caller_number) <= 32);
CREATE INDEX tool_invocations_lookup_caller_idx
  ON tool_invocations (clinic_id, caller_number, created_at)
  WHERE tool = 'lookup_patient';
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
