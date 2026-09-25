# Runbook: a clinic's team

Admins and owners manage who can see a clinic's calls from the dashboard's **Team** page. Nothing
is added by SQL any more, except the very first owner of a new clinic (go-live §3).

## How access works

- A member's **role** is `viewer` (sees calls and the TV), `receptionist` (works the inbox),
  `admin` (plus usage and the team) or `owner` (plus making owners).
- **Inviting:** an admin enters an email and a role. The invitation is open until that person
  signs in with that email (verified by the sign-in provider) and is then turned into a
  membership by their own sign-in, under the invited clinic's row-level security. Invitations
  never grant access by themselves; the email must sign in.
- **Rank rules:** nobody grants a role above their own; only an owner makes an owner; you can
  only change or remove people at or below your rank; a clinic always keeps at least one owner.
- **Removal or demotion takes effect immediately** for the API (memberships are read on every
  request) and within five minutes for live screens (streams re-check membership every 5 minutes
  and end with `revoked`).
- Every change is written to the clinic's audit log (`invitation.created`, `invitation.revoked`,
  `membership.accepted`, `membership.role_changed`, `membership.removed`).

## Sign-in itself

Accounts live in the sign-in provider (Firebase Authentication). Removing someone from every
clinic removes their access to everything in WASSUP; deleting their sign-in account is done in
the provider's console and is not required. Multi-factor authentication for admins and owners is
an owner item in the provider (Identity Platform TOTP); the API does not yet enforce it.

## First owner of a new clinic

Until the clinic has an owner nobody can invite anyone, and an invitation can't create the first
owner (an invitation is accepted only if its inviter still ranks at or above the invited role).
The first owner is added with the clinic itself: `db-admin` `WASSUP_ROLE=onboard-clinic`
(go-live §3), from their sign-in account id and email.
