/** The signed-in person's API client and clinic list (from /v1/me), shared by every page. */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Api, ApiError } from "../api/client";
import type { ClinicAccess, Me } from "../api/types";
import { useAuth } from "./auth";

interface Session {
  api: Api;
  me: Me | null;
  error: "no_access" | "unavailable" | null;
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
    api
      .me()
      .then((m) => {
        if (!cancelled) {
          setMe(m);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError && (e.status === 403 || e.status === 404) ? "no_access" : "unavailable");
      });
    return () => {
      cancelled = true;
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
