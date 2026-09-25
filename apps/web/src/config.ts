/** Build-time configuration (Vite env). Nothing here is secret: it all ships to the browser. */

export type AuthMode = "firebase" | "test";

function required(name: string, value: string | undefined): string {
  if (!value) throw new Error(`Missing build setting ${name}`);
  return value;
}

const env = import.meta.env;

export const config = {
  apiBase: (env.VITE_API_BASE ?? "").replace(/\/$/, ""),
  authMode: (env.VITE_AUTH_MODE === "test" ? "test" : "firebase") as AuthMode,
  firebase: {
    apiKey: env.VITE_FIREBASE_API_KEY as string | undefined,
    authDomain: env.VITE_FIREBASE_AUTH_DOMAIN as string | undefined,
    projectId: env.VITE_FIREBASE_PROJECT_ID as string | undefined,
    appId: env.VITE_FIREBASE_APP_ID as string | undefined,
  },
};

/** Shapes of values that are easy to paste into the wrong setting. */
const SHAPES: Record<string, RegExp> = {
  VITE_API_BASE: /^https?:\/\/[^/\s]+$/,
  VITE_FIREBASE_API_KEY: /^AIza[0-9A-Za-z_-]{35}$/,
  VITE_FIREBASE_AUTH_DOMAIN: /^[a-z0-9.-]+\.[a-z]{2,}$/,
  VITE_FIREBASE_APP_ID: /^\d+:\d+:web:[0-9a-f]+$/,
};

/**
 * Build settings that are missing, or that hold something else (a domain pasted where the API
 * key goes, say), each named for the administrator. Empty when the build is complete. Nothing
 * here is secret, but the values are never shown: only which setting to fix.
 */
export function settingProblems(): string[] {
  const settings: [string, string | undefined][] =
    config.authMode === "test"
      ? [["VITE_API_BASE", config.apiBase]]
      : [
          ["VITE_API_BASE", config.apiBase],
          ["VITE_FIREBASE_API_KEY", config.firebase.apiKey],
          ["VITE_FIREBASE_AUTH_DOMAIN", config.firebase.authDomain],
          ["VITE_FIREBASE_PROJECT_ID", config.firebase.projectId],
          ["VITE_FIREBASE_APP_ID", config.firebase.appId],
        ];
  return settings.flatMap(([name, value]) => {
    if (!value) return [`${name} is not set`];
    const shape = SHAPES[name];
    return shape && !shape.test(value) ? [`${name} doesn't look right`] : [];
  });
}

export function firebaseOptions() {
  return {
    apiKey: required("VITE_FIREBASE_API_KEY", config.firebase.apiKey),
    authDomain: required("VITE_FIREBASE_AUTH_DOMAIN", config.firebase.authDomain),
    projectId: required("VITE_FIREBASE_PROJECT_ID", config.firebase.projectId),
    appId: required("VITE_FIREBASE_APP_ID", config.firebase.appId),
  };
}
