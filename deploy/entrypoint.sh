#!/bin/sh
# Container entrypoint for every service image. Behaviour comes from the environment, so it works
# on any platform with or without config-as-code:
#
#   WASSUP_ROLE=serve (default)   run this image's service (SERVICE_MODULE, baked in at build)
#   WASSUP_ROLE=bootstrap         run db/bootstrap.py once (roles + passwords) and exit
#   WASSUP_ROLE=seed-synthetic    seed a synthetic clinic (refused in production) and exit
#   WASSUP_ROLE=report            print counts only (no personal data) and exit
#   WASSUP_ROLE=import-legacy     import one clinic's legacy history (db/import_legacy.py; a
#                                 dry run unless WASSUP_IMPORT_APPLY=true) and exit
#   WASSUP_ROLE=import-patients   upsert one clinic's patient list (db/import_patients.py; a dry
#                                 run unless WASSUP_PATIENTS_APPLY=true) and exit
#   WASSUP_ROLE=load-classifier-rules | activate-classifier-rules | reclassify
#                                 a clinic's classification rules (db/classifier_rules.py) and exit
#   WASSUP_ROLE=backup            one verified encrypted backup now (ops_worker.backup) and exit
#   WASSUP_ROLE=ops               an operator decision from a runbook (db/ops_actions.py: list,
#                                 requeue or abandon dead outbox events, resolve quarantine; a dry
#                                 run unless WASSUP_OPS_APPLY=true) and exit
#   WASSUP_ROLE=onboard-clinic    create a clinic and its first owner (db/onboard_clinic.py; a
#                                 dry run unless WASSUP_CLINIC_APPLY=true) and exit
#   WASSUP_ROLE=restore           restore an archive into THIS database (db/restore.py; a dry run
#                                 unless WASSUP_RESTORE_APPLY=true) and exit
#   WASSUP_MIGRATE_ON_START=true  (serve) apply migrations before starting — for platforms where
#                                 the image's pre-deploy step isn't configured. Migrations hold an
#                                 advisory lock, so concurrent starts are safe.
set -eu

case "${WASSUP_ROLE:-serve}" in
  bootstrap)
    exec python db/bootstrap.py
    ;;
  seed-synthetic)
    exec python db/seed_synthetic.py
    ;;
  report)
    exec python db/report.py
    ;;
  import-legacy)
    exec python db/import_legacy.py
    ;;
  import-patients)
    exec python db/import_patients.py
    ;;
  load-classifier-rules|activate-classifier-rules|reclassify)
    exec python db/classifier_rules.py
    ;;
  backup)
    exec python -m ops_worker.backup
    ;;
  restore)
    exec python db/restore.py
    ;;
  ops)
    exec python db/ops_actions.py
    ;;
  onboard-clinic)
    exec python db/onboard_clinic.py
    ;;
  serve)
    if [ "${WASSUP_MIGRATE_ON_START:-false}" = "true" ]; then
      alembic -c db/alembic.ini upgrade head
    fi
    module="$(echo "${SERVICE_MODULE}" | tr - _).main:app"
    # 30 s graceful shutdown so in-flight voice tool calls finish during a deploy. uvicorn's own
    # access log is off: our structured request log never records query strings.
    exec uvicorn "$module" --host 0.0.0.0 --port "${PORT:-8080}" \
      --timeout-graceful-shutdown 30 --no-server-header --no-access-log
    ;;
  *)
    echo "unknown WASSUP_ROLE: ${WASSUP_ROLE}" >&2
    exit 2
    ;;
esac
