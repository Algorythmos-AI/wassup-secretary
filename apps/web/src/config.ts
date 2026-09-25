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

export function firebaseOptions() {
  return {
    apiKey: required("VITE_FIREBASE_API_KEY", config.firebase.apiKey),
    authDomain: required("VITE_FIREBASE_AUTH_DOMAIN", config.firebase.authDomain),
    projectId: required("VITE_FIREBASE_PROJECT_ID", config.firebase.projectId),
    appId: required("VITE_FIREBASE_APP_ID", config.firebase.appId),
  };
}
