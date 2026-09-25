import { useAuth } from "../auth/auth";
import "../styles/signin.css";

export function NoAccess({ reason }: { reason: "no_access" | "unavailable" }) {
  const { email, signOut } = useAuth();
  return (
    <main className="signin">
      <div className="signin__sheet">
        <h1 className="signin__product">WASSUP</h1>
        {reason === "no_access" ? (
          <p>
            {email} isn't linked to a clinic yet. Ask your practice manager to add you, then sign in again.
          </p>
        ) : (
          <p>Can't reach WASSUP right now. Check the connection, then reload the page.</p>
        )}
        <button type="button" className="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </div>
    </main>
  );
}
