/**
 * Staff sign-in. Firebase Authentication in every real environment (Google or email + password,
 * as clinic staff use today); a local-only "test" mode issues core-api test tokens for development
 * and is refused by core-api anywhere but local/test.
 */
import { initializeApp } from "firebase/app";
import {
  GoogleAuthProvider,
  getAuth,
  onIdTokenChanged,
  signInWithEmailAndPassword,
  signInWithPopup,
  signOut as firebaseSignOut,
  type Auth,
} from "firebase/auth";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { config, firebaseOptions } from "../config";

export interface AuthContextValue {
  status: "loading" | "signed-out" | "signed-in";
  email: string | null;
  mode: "firebase" | "test";
  token: (forceRefresh?: boolean) => Promise<string>;
  signInWithGoogle: () => Promise<void>;
  signInWithEmail: (email: string, password: string) => Promise<void>;
  signInForTest: (uid: string, email: string) => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);
const TEST_KEY = "wassup.testIdentity";

let firebaseAuth: Auth | null = null;
function auth(): Auth {
  firebaseAuth ??= getAuth(initializeApp(firebaseOptions()));
  return firebaseAuth;
}

function readTestIdentity(): { uid: string; email: string } | null {
  try {
    const raw = sessionStorage.getItem(TEST_KEY);
    return raw ? (JSON.parse(raw) as { uid: string; email: string }) : null;
  } catch {
    return null;
  }
}

/** Plain-language messages for the sign-in failures people actually hit. */
export function signInMessage(error: unknown): string | null {
  const code = (error as { code?: string } | null)?.code ?? "";
  switch (code) {
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return null;
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
    case "auth/invalid-email":
      return "That email and password don't match an account.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a minute, then try again.";
    case "auth/unauthorized-domain":
      return "Sign-in isn't enabled for this web address yet. Ask your administrator to allow it.";
    case "auth/network-request-failed":
      return "Can't reach the sign-in service. Check the connection and try again.";
    default:
      return "Sign-in didn't work. Try again, or use the other sign-in option.";
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const mode = config.authMode;
  const [state, setState] = useState<{ status: AuthContextValue["status"]; email: string | null }>(
    () => {
      if (mode !== "test") return { status: "loading", email: null };
      const identity = readTestIdentity();
      return identity ? { status: "signed-in", email: identity.email } : { status: "signed-out", email: null };
    },
  );

  useEffect(() => {
    if (mode === "test") return;
    return onIdTokenChanged(auth(), (user) => {
      const next = user ? { status: "signed-in" as const, email: user.email } : { status: "signed-out" as const, email: null };
      // Hourly token refreshes fire here too: only re-render when who is signed in changes.
      setState((prev) => (prev.status === next.status && prev.email === next.email ? prev : next));
    });
  }, [mode]);

  // Stable across renders and token refreshes, so the API client built on it stays the same.
  const token = useCallback(
    async (forceRefresh = false) => {
      if (mode === "test") {
        const identity = readTestIdentity();
        if (!identity) throw new Error("signed out");
        return `test:${identity.uid}:${identity.email}`;
      }
      const user = auth().currentUser;
      if (!user) throw new Error("signed out");
      return user.getIdToken(forceRefresh);
    },
    [mode],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      mode,
      token,
      async signInWithGoogle() {
        await signInWithPopup(auth(), new GoogleAuthProvider());
      },
      async signInWithEmail(email, password) {
        await signInWithEmailAndPassword(auth(), email.trim(), password);
      },
      signInForTest(uid, email) {
        if (mode !== "test") return;
        try {
          sessionStorage.setItem(TEST_KEY, JSON.stringify({ uid, email }));
        } catch {
          /* private mode: still signed in for this page */
        }
        setState({ status: "signed-in", email });
      },
      async signOut() {
        if (mode === "test") {
          try {
            sessionStorage.removeItem(TEST_KEY);
          } catch {
            /* ignore */
          }
          setState({ status: "signed-out", email: null });
          return;
        }
        await firebaseSignOut(auth());
      },
    }),
    [state, mode, token],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}
