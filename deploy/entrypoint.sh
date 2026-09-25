#!/bin/sh
# Container entrypoint for every service image. Behaviour comes from the environment, so it works
# on any platform with or without config-as-code:
#
#   WASSUP_ROLE=serve (default)   run this image's service (SERVICE_MODULE, baked in at build)
#   WASSUP_ROLE=bootstrap         run db/bootstrap.py once (roles + passwords) and exit
#   WASSUP_MIGRATE_ON_START=true  (serve) apply migrations before starting — for platforms where
#                                 the image's pre-deploy step isn't configured. Migrations hold an
#                                 advisory lock, so concurrent starts are safe.
set -eu

case "${WASSUP_ROLE:-serve}" in
  bootstrap)
    exec python db/bootstrap.py
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
