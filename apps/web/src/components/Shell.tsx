import { NavLink, Outlet, useNavigate, useParams } from "react-router";
import { useAuth } from "../auth/auth";
import { canSeeUsage, useSession } from "../auth/session";
import "../styles/shell.css";

export function Shell() {
  const { clinicId } = useParams();
  const { me, clinic } = useSession();
  const { email, signOut } = useAuth();
  const navigate = useNavigate();
  const current = clinic(clinicId);
  if (!me || !current) return null;

  return (
    <div className="shell">
      <aside className="rail" aria-label="Clinic and pages">
        <div className="rail__brand">
          <span className="rail__product">WASSUP</span>
          <span className="rail__subtitle">Reception</span>
        </div>
        {me.clinics.length > 1 ? (
          <label className="rail__clinic">
            <span className="visually-hidden">Clinic</span>
            <select
              value={current.id}
              onChange={(e) => navigate(`/c/${e.target.value}/inbox`)}
              aria-label="Clinic"
            >
              {me.clinics.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div className="rail__clinic rail__clinic--single">{current.name}</div>
        )}
        <nav className="rail__nav">
          <NavLink to={`/c/${current.id}/inbox`}>Inbox</NavLink>
          <NavLink to={`/c/${current.id}/analytics`}>Analytics</NavLink>
          {canSeeUsage(current.role) && <NavLink to={`/c/${current.id}/usage`}>Usage</NavLink>}
          <a href={`/c/${current.id}/tv`} target="_blank" rel="noopener">
            Office TV
          </a>
        </nav>
        <div className="rail__account">
          <span className="rail__email" title={email ?? ""}>
            {email}
          </span>
          <button className="rail__signout" type="button" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="shell__main">
        <Outlet context={current} />
      </main>
    </div>
  );
}
