# Runbook: a clinic's classification rules

Every analysed call gets a priority tier (`emergency`, `priority_1`, `priority_2`, `priority_3`,
`none`), an "is this a reception action" flag and a label for what the caller wanted. The engine
(`libs/wassup_core/src/wassup_core/classify.py`) is generic; **which words mean what for a clinic
is that clinic's own data**, kept as a versioned JSON document in `clinic_classifier_rules` and
never in this repository. The schema is documented in the engine's models (`RuleSet`).

Until a clinic has active rules, its calls are stored unclassified (tier `null`, priority false).
Nothing is ever dropped because of rules: a rule set the engine can't read is logged and ignored.

## Load a new version (dry, then live)

Rules are loaded inside the platform by the one-shot `db-admin` service, so the document goes
from the owner's clipboard into the database and nowhere else.

1. Set on `db-admin` in the target environment:
   - `WASSUP_ROLE=load-classifier-rules`
   - `WASSUP_RULES_CLINIC=<slug>`
   - `WASSUP_RULES_JSON=<the JSON document>`
   - `WASSUP_RULES_NOTE=<what changed>` (optional)
   - leave `WASSUP_RULES_ACTIVATE` unset: the version is stored **inactive**.
2. Deploy `db-admin`. The log ends `clinic <slug>: rules version N stored`. An invalid document is
   refused before anything is written (`rules refused: ValidationError`).
3. Check the version against the clinic's history without touching live calls: run the parity
   script from the private legacy repository, which classifies the clinic's calls with this
   document and reports counts only.
4. Activate: `WASSUP_ROLE=activate-classifier-rules`, `WASSUP_RULES_VERSION=N`, deploy `db-admin`.
   Activation reclassifies every analysed call of that clinic in one transaction and writes one
   `audit_log` row (`calls.reclassify`) with the counts. New calls use the active version from the
   next minute (voice-gateway caches rules for 60 s).
5. Clean up: remove `WASSUP_RULES_JSON`, set `WASSUP_ROLE=report`.

## Roll back

`WASSUP_ROLE=activate-classifier-rules` with the previous `WASSUP_RULES_VERSION`. Same
reclassification, same audit row. Versions are never deleted.

## Reclassify without changing rules

`WASSUP_ROLE=reclassify` (with `WASSUP_RULES_CLINIC`, or without it for every clinic with active
rules). Idempotent: a second run reports `changed=0`.

## What the tiers mean on screen

- `is_priority` = tier is `emergency`, `priority_1` or `priority_2`: the call is shown as a priority
  in the inbox, on the office TV, and in the analytics count.
- `action_label` replaces the raw intent in the call list and on the TV when present.
- `priority_reason` is stored for operators and is not shown to reception.

## Where the words come from

For the clinics on the legacy dashboard, the private repository's `scripts/export-classifier-rules.mjs`
turns its classifier constants into this document, and `scripts/classifier-parity.mjs` proves the
document reproduces the legacy classifier's tier on the clinic's real history (zero differences)
before it is activated here.
