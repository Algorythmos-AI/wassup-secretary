# ADR 0007 — Fresh-history repository, temporarily public

**Status:** Accepted (owner decision, 2026-09-25)

## Context
Private-repo CI minutes were exhausted; public repositories run GitHub Actions for free. The legacy
repository's history contains a credential and must never be published.

## Decision
- This repository starts with fresh history and is public **temporarily** under a proprietary
  licence and `NOTICE.md` (`intended_visibility: private` in the org catalog).
- While public it contains no secrets, no real clinic or patient data, no production clinic
  configuration and no voice-agent prompts, and nothing is copied from any private repository.
- Secret scanning (gitleaks) runs in pre-commit and CI.

## Consequences
Domain rules and prompts are re-specified here only once the repository is private again, or kept in
private companion repositories. Going private follows the org's `go-private` runbook.
