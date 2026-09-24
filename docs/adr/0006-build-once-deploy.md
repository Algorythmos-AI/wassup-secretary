# ADR 0006 — Build once, promote the same image; CI drives deploys

**Status:** Accepted

## Decision
- CI builds each service image once per source tree, tagged by the git tree hash, and the same
  digest is promoted from staging to production. `/health` reports version and tree.
- Deploys run from CI after every required check on that exact commit is green; platform
  auto-deploy is off so a red build can never ship. Rollback = redeploy the previous digest.
- Migrations are forward-only (expand → contract over two releases), run by ops-worker's pre-deploy
  step as the migrator role under an advisory lock and a short `lock_timeout`.
- Branches: feature → `integration` (staging) → release PR → `main` (production), tagged releases.
