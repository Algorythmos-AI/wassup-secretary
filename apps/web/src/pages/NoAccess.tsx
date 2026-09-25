import { useAuth } from "../auth/auth";
import "../styles/signin.css";

export function NoAccess({ reason }: { reason: "no_access" | "not_accepted" | "unavailable" }) {
  const { email, signOut } = useAuth();
  return (
    <main className="signin">
      <div className="signin__sheet">
        <h1 className="signin__product">WASSUP</h1>
        {reason === "no_access" ? (
          <p>
            {email} isn't linked to a clinic yet. Ask your practice manager to add you, then sign in again.
          </p>
        ) : reason === "not_accepted" ? (
          <p>
            Your sign-in for {email} wasn't accepted. If you sign in with an email and password, your
            email address needs to be verified first: ask your practice manager to resend the invitation.
          </p>
        ) : (
          <p role="status">Can't reach WASSUP right now. Trying again automatically…</p>
        )}
        <button type="button" className="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </div>
    </main>
  );
}
