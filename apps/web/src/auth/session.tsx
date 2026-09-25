/** The signed-in person's API client and clinic list (from /v1/me), shared by every page. */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Api, ApiError } from "../api/client";
import type { ClinicAccess, Me } from "../api/types";
import { useAuth } from "./auth";

interface Session {
  api: Api;
  me: Me | null;
  /** no_access: not a member of any clinic. not_accepted: the sign-in token is rejected even
   * after a refresh (e.g. an email address not yet verified). unavailable: can't reach the API
   * right now; it keeps retrying. */
  error: "no_access" | "not_accepted" | "unavailable" | null;
  clinic: (id: string | undefined) => ClinicAccess | undefined;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const { token, status } = useAuth();
  const api = useMemo(() => new Api(token), [token]);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<Session["error"]>(null);

  useEffect(() => {
    if (status !== "signed-in") {
      setMe(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = 2000;
    // An office TV can boot before its network is up, or during an API deploy: keep trying,
    // more slowly each time, until it works. Only a definite answer stops it.
    const attempt = () => {
      api
        .me()
        .then((m) => {
          if (cancelled) return;
          setMe(m);
          setError(null);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          const code = e instanceof ApiError ? e.status : 0;
          if (code === 403 || code === 404) return setError("no_access");
          if (code === 401) return setError("not_accepted");
          setError("unavailable");
          timer = setTimeout(attempt, delay);
          delay = Math.min(delay * 2, 60_000);
        });
    };
    attempt();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [api, status]);

  const value = useMemo<Session>(
    () => ({ api, me, error, clinic: (id) => me?.clinics.find((c) => c.id === id) }),
    [api, me, error],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}

export const canChangeStatus = (role: ClinicAccess["role"]) => role !== "viewer";
export const canSeeUsage = (role: ClinicAccess["role"]) => role === "admin" || role === "owner";
