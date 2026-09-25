import { useState, type FormEvent } from "react";
import { signInMessage, useAuth } from "../auth/auth";
import "../styles/signin.css";

export function SignIn() {
  const { mode, signInWithGoogle, signInWithEmail, signInForTest } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function attempt(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(signInMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function onEmail(event: FormEvent) {
    event.preventDefault();
    if (mode === "test") {
      signInForTest(email.split("@")[0] || "local", email);
      return;
    }
    void attempt(() => signInWithEmail(email, password));
  }

  return (
    <main className="signin">
      <div className="signin__sheet">
        <h1 className="signin__product">WASSUP</h1>
        <p className="signin__lede">Sign in to see the calls your AI receptionist took.</p>
        {mode === "firebase" && (
          <button type="button" className="button signin__google" disabled={busy} onClick={() => void attempt(signInWithGoogle)}>
            Sign in with Google
          </button>
        )}
        <form className="signin__form" onSubmit={onEmail}>
          <label>
            Email
            <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {mode === "firebase" && (
            <label>
              Password
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
          )}
          <button type="submit" className="button button--primary" disabled={busy}>
            {mode === "test" ? "Sign in (local test mode)" : "Sign in with email"}
          </button>
        </form>
        {error && (
          <p className="notice notice--error" role="alert">
            {error}
          </p>
        )}
      </div>
    </main>
  );
}
