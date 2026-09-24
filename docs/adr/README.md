# Architecture Decision Records

One file per decision: context, decision, consequences. Superseded ADRs stay and link forward.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-python-fastapi.md) | Python + FastAPI for the new platform | Accepted |
| [0002](0002-three-services.md) | Three services and one shared library — no more | Accepted |
| [0003](0003-tenancy-rls.md) | Clinic isolation enforced by Postgres row-level security | Accepted |
| [0004](0004-raw-event-first-ingestion.md) | Store raw provider events before processing; idempotent tools | Accepted |
| [0005](0005-transactional-outbox.md) | Transactional outbox; NOTIFY is only a wake-up hint | Accepted |
| [0006](0006-build-once-deploy.md) | Build once, promote the same image; CI drives deploys | Accepted |
| [0007](0007-public-fresh-history-repo.md) | Fresh-history repo, temporarily public, nothing copied from private repos | Accepted |
| [0008](0008-data-residency.md) | Hosting region and cross-border disclosure | Proposed |
| [0009](0009-separate-database.md) | New database; legacy history imported by a private ETL | Proposed |
| [0010](0010-replay-through-own-endpoint.md) | Truthful tool fallbacks; recover unfinished work by replaying through voice-gateway's own endpoint | Accepted |
| [0011](0011-watch-the-providers-and-stream-by-polling.md) | Continuous external-configuration monitors; live events by polling | Accepted |
