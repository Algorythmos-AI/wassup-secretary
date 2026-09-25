#!/usr/bin/env bash
# Deploy exact commits of this repository to a Railway environment, one service at a time.
#
#   scripts/deploy-railway.sh <environment> <service> [<service> ...]
#   e.g. scripts/deploy-railway.sh staging db-admin ops-worker voice-gateway core-api
#
# For each service: export HEAD with `git archive` (only committed files, nothing local), put that
# service's deploy/railway/<service>.json at the root as railway.json (Railway reads it natively:
# Dockerfile build, health check, replicas, draining, pre-deploy migrations), record the commit as
# build identity (/health reports it), and `railway up` that directory. Order matters: db-admin
# (roles) before ops-worker (migrations, in its pre-deploy step) before the other services.
set -euo pipefail

env="${1:?usage: $0 <environment> <service>...}"
shift
[ "$#" -gt 0 ] || { echo "no services given" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "uncommitted changes: commit or stash first (deploys must be exact commits)" >&2
  exit 2
fi
sha="$(git rev-parse HEAD)"
tree="$(git rev-parse 'HEAD^{tree}')"

# Production only ever runs a release: an exact v* tag whose commit is on origin/main. Anything
# else (a feature branch, integration, an untagged fix) is refused here, before any upload.
if [ "$env" = "production" ]; then
  tag="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
  case "$tag" in
    v[0-9]*) ;;
    *) echo "production deploys need HEAD to be an exact v* tag (got '${tag:-none}'): cut a release first" >&2; exit 2 ;;
  esac
  git fetch -q origin main
  if ! git merge-base --is-ancestor "$sha" origin/main; then
    echo "production deploys must come from main: $tag ($sha) is not on origin/main" >&2; exit 2
  fi
  grep -q "^## \[${tag#v}\]" CHANGELOG.md || { echo "CHANGELOG.md has no '## [${tag#v}]' section" >&2; exit 2; }
fi

for service in "$@"; do
  config="deploy/railway/${service}.json"
  [ -f "$config" ] || { echo "missing $config" >&2; exit 2; }
  dir="$(mktemp -d)"
  trap 'rm -rf "$dir"' EXIT
  git archive "$sha" | tar -x -C "$dir"
  cp "$config" "$dir/railway.json"
  echo "==> $service: ${sha:0:12} (tree ${tree:0:12}) to $env"
  railway variable set "VERSION=${sha:0:12}" "GIT_TREE=${tree}" -s "$service" -e "$env" --skip-deploys >/dev/null
  railway up "$dir" --path-as-root -s "$service" -e "$env" --ci -m "${service} ${sha:0:12}"
  rm -rf "$dir"
  trap - EXIT
done
