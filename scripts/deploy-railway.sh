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
  # RELEASE_TAG (set by the release workflow) must point at HEAD; otherwise any v* tag on HEAD.
  tag="${RELEASE_TAG:-$(git tag --points-at HEAD | grep -m1 '^v[0-9]' || true)}"
  case "$tag" in
    v[0-9]*) ;;
    *) echo "production deploys need HEAD to be an exact v* tag (got '${tag:-none}'): cut a release first" >&2; exit 2 ;;
  esac
  if [ "$(git rev-parse "refs/tags/${tag}^{commit}" 2>/dev/null)" != "$sha" ]; then
    echo "tag $tag does not point at HEAD ($sha)" >&2; exit 2
  fi
  git fetch -q origin main
  if ! git merge-base --is-ancestor "$sha" origin/main; then
    echo "production deploys must come from main: $tag ($sha) is not on origin/main" >&2; exit 2
  fi
  grep -qF "## [${tag#v}]" CHANGELOG.md || { echo "CHANGELOG.md has no '## [${tag#v}]' section" >&2; exit 2; }
fi

# `railway up --ci` returns when the BUILD finishes, not when the deploy is live. Each service
# must be up (and, when it has a public domain, reporting this commit's tree) before the next one
# starts: db-admin's role bootstrap before ops-worker's migrations, migrations before the rest.
wait_live() {
  local service="$1" deadline=$((SECONDS + 900)) status domain path got
  while :; do
    status="$(railway deployment list -s "$service" -e "$env" 2>/dev/null | sed -n '2p' | awk -F'|' '{gsub(/ /,"",$2); print $2}')"
    case "$status" in
      SUCCESS) break ;;
      FAILED|CRASHED|REMOVED) echo "$service deployment $status" >&2; return 1 ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || { echo "$service: still '$status' after 15 min" >&2; return 1; }
    sleep 10
  done
  domain="$(railway variable list -s "$service" -e "$env" --kv 2>/dev/null | sed -n 's/^RAILWAY_PUBLIC_DOMAIN=//p')"
  [ -n "$domain" ] || return 0
  path=/health; [ "$service" = web ] && path=/version.json
  while :; do
    got="$(curl -fsS -m 10 "https://$domain$path" 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("tree") or d.get("build") or "")' 2>/dev/null || true)"
    [ "$got" = "$tree" ] && { echo "    $service live on $domain"; return 0; }
    [ "$SECONDS" -lt "$deadline" ] || { echo "$service on $domain reports '$got', expected $tree" >&2; return 1; }
    sleep 10
  done
}

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
  wait_live "$service"
done
