"""Per-clinic classification rules, and the classification stored on each call.

The engine (``wassup_core.classify``) is generic and public; which words mean "urgent" for a clinic
are that clinic's own data. ``clinic_classifier_rules`` holds one validated JSON document per
version per clinic, with at most one active version, so a bad rule set is rolled back by
activating the previous version. Each call records the level it was given, when, and by which
rules version, so a reclassification is auditable and idempotent.

``is_priority`` and ``is_reception_action`` already existed (always false until now). They are
now set by the engine at write time and can be backfilled (``db/classifier_rules.py`` (``WASSUP_ROLE=reclassify``)).

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE clinic_classifier_rules (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id   uuid NOT NULL REFERENCES clinics(id),
  version     integer NOT NULL CHECK (version > 0),
  rules       jsonb NOT NULL,
  active      boolean NOT NULL DEFAULT false,
  note        text CHECK (char_length(note) <= 500),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, version)
);
CREATE UNIQUE INDEX clinic_classifier_rules_one_active ON clinic_classifier_rules (clinic_id) WHERE active;
ALTER TABLE clinic_classifier_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_classifier_rules FORCE ROW LEVEL SECURITY;
CREATE POLICY clinic_classifier_rules_clinic_isolation ON clinic_classifier_rules
  USING (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]))
  WITH CHECK (clinic_id = ANY ((SELECT wassup_current_clinics())::uuid[]));
-- Only the voice gateway reads rules (they are clinic vocabulary, and only ingestion needs
-- them); versions are written by the owner role (db-admin).
GRANT SELECT ON clinic_classifier_rules TO app_voice;

ALTER TABLE calls
  -- The agent's own routing decision and the provider's success judgement: inputs to the
  -- classifier that must be kept, so a later reclassification sees what the webhook saw.
  ADD COLUMN triage_route text CHECK (char_length(triage_route) <= 60),
  ADD COLUMN call_successful boolean,
  ADD COLUMN priority_level text
    CHECK (priority_level IN ('emergency','priority_1','priority_2','priority_3','none')),
  ADD COLUMN priority_reason text CHECK (char_length(priority_reason) <= 60),
  ADD COLUMN action_label text CHECK (char_length(action_label) <= 60),
  ADD COLUMN classified_at timestamptz,
  ADD COLUMN classifier_version integer;
CREATE INDEX calls_clinic_priority_idx ON calls (clinic_id, priority_level)
  WHERE priority_level IN ('emergency', 'priority_1', 'priority_2');
"""


def upgrade() -> None:
    op.execute("SET ROLE wassup_owner")
    op.execute(SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Forward-only: roll back by redeploying the previous image.")
