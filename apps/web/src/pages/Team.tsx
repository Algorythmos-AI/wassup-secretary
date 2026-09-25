/** Who works at this clinic: invite by email, change roles, remove. Admins and owners only. */
import { useEffect, useState, type FormEvent } from "react";
import { Navigate, useOutletContext } from "react-router";
import { ApiError } from "../api/client";
import type { ClinicAccess, Role, Team as TeamData } from "../api/types";
import { useAuth } from "../auth/auth";
import { canSeeUsage, useSession } from "../auth/session";
import "../styles/team.css";

const ROLE_LABEL: Record<Role, string> = {
  viewer: "Viewer",
  receptionist: "Receptionist",
  admin: "Admin",
  owner: "Owner",
};
const ROLE_HELP: Record<Role, string> = {
  viewer: "Sees calls and the office TV. Can't change anything.",
  receptionist: "Works the inbox: marks calls done, adds notes.",
  admin: "Everything above, plus usage and the team.",
  owner: "Everything, including making other owners.",
};
const ROLE_RANK: Record<Role, number> = { viewer: 0, receptionist: 1, admin: 2, owner: 3 };

/** Roles this person may give: never above their own, and only owners make owners. */
function grantable(own: Role): Role[] {
  return (Object.keys(ROLE_RANK) as Role[]).filter((r) => ROLE_RANK[r] <= ROLE_RANK[own] && (r !== "owner" || own === "owner"));
}

function problem(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409) return e.message;
    if (e.status === 403) return e.message || "You can't do that with your role.";
    if (e.status === 404) return "That person or invitation is no longer here.";
  }
  return "That didn't save. Try again.";
}

export function Team() {
  const clinic = useOutletContext<ClinicAccess>();
  const { api } = useSession();
  const { email: myEmail } = useAuth();
  const [team, setTeam] = useState<TeamData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<Role>("receptionist");
  const allowed = canSeeUsage(clinic.role);

  useEffect(() => {
    if (!allowed) return;
    let cancelled = false;
    api
      .team(clinic.id)
      .then((t) => !cancelled && (setTeam(t), setError(null)))
      .catch(() => !cancelled && setError("Couldn't load the team. Try again in a moment."));
    return () => {
      cancelled = true;
    };
  }, [api, clinic.id, allowed]);

  if (!allowed) return <Navigate to={`/c/${clinic.id}/inbox`} replace />;

  async function act(what: () => Promise<{ team: TeamData }>, done: string) {
    setBusy(true);
    setNotice(null);
    try {
      const result = await what();
      setTeam(result.team);
      setNotice(done);
    } catch (e) {
      setNotice(problem(e));
    } finally {
      setBusy(false);
    }
  }

  function onInvite(event: FormEvent) {
    event.preventDefault();
    const email = inviteEmail.trim();
    if (!email) return;
    void act(() => api.invite(clinic.id, email, inviteRole), `Invited ${email}. They'll see ${clinic.name} the next time they sign in.`).then(() => setInviteEmail(""));
  }

  const roles = grantable(clinic.role);
  return (
    <div className="team">
      <header className="page-header">
        <h1>Team</h1>
      </header>
      <p className="team__lede">People who can see {clinic.name}'s calls. Removing someone ends their access straight away.</p>
      {error && <p className="notice notice--error">{error}</p>}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}

      <form className="team__invite" onSubmit={onInvite}>
        <label>
          Email
          <input type="email" required value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} placeholder="name@clinic.example" />
        </label>
        <label>
          Role
          <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as Role)}>
            {roles.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="button button--primary" disabled={busy}>
          Invite
        </button>
        <p className="team__help">{ROLE_HELP[inviteRole]}</p>
      </form>

      {team && (
        <>
          <section aria-label="Members">
            <h2>Members</h2>
            <table className="team__table">
              <thead>
                <tr>
                  <th scope="col">Person</th>
                  <th scope="col">Role</th>
                  <th scope="col">Since</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {team.members.map((m) => {
                  const isMe = !!myEmail && m.email.toLowerCase() === myEmail.toLowerCase();
                  const canTouch = ROLE_RANK[m.role] <= ROLE_RANK[clinic.role];
                  return (
                    <tr key={m.staff_user_id}>
                      <td>
                        {m.display_name ? `${m.display_name} ` : ""}
                        <span className="team__email">{m.email}</span>
                        {isMe && <span className="team__you">you</span>}
                      </td>
                      <td>
                        {canTouch ? (
                          <select
                            aria-label={`Role for ${m.email}`}
                            value={m.role}
                            disabled={busy}
                            onChange={(e) => void act(() => api.setRole(clinic.id, m.staff_user_id, e.target.value as Role), `${m.email} is now ${ROLE_LABEL[e.target.value as Role].toLowerCase()}.`)}
                          >
                            {roles.includes(m.role) ? roles.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>) : <option value={m.role}>{ROLE_LABEL[m.role]}</option>}
                          </select>
                        ) : (
                          ROLE_LABEL[m.role]
                        )}
                      </td>
                      <td>{new Date(m.since).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" })}</td>
                      <td>
                        {canTouch && (
                          <button
                            type="button"
                            className="button button--quiet"
                            disabled={busy}
                            onClick={() => {
                              if (window.confirm(`Remove ${m.email} from ${clinic.name}? Their access ends straight away.`)) {
                                void act(() => api.removeMember(clinic.id, m.staff_user_id), `${m.email} no longer has access.`);
                              }
                            }}
                          >
                            Remove
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>

          <section aria-label="Invitations">
            <h2>Invited, not yet signed in</h2>
            {team.invitations.length === 0 ? (
              <p className="team__quiet">No open invitations.</p>
            ) : (
              <ul className="team__invitations">
                {team.invitations.map((i) => (
                  <li key={i.id}>
                    <span className="team__email">{i.email}</span> as {ROLE_LABEL[i.role].toLowerCase()}
                    <button type="button" className="button button--quiet" disabled={busy} onClick={() => void act(() => api.revokeInvitation(clinic.id, i.id), `Invitation for ${i.email} withdrawn.`)}>
                      Withdraw
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
