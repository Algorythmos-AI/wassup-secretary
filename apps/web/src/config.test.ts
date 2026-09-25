import { afterEach, expect, it, vi } from "vitest";

const good = {
  VITE_API_BASE: "https://core-api.example.test",
  VITE_FIREBASE_API_KEY: `AIza${"x".repeat(35)}`,
  VITE_FIREBASE_AUTH_DOMAIN: "example-project.firebaseapp.com",
  VITE_FIREBASE_PROJECT_ID: "example-project",
  VITE_FIREBASE_APP_ID: "1:1234567890:web:abc123",
};

async function problemsWith(env: Record<string, string>) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
  const { settingProblems } = await import("./config");
  return settingProblems();
}

afterEach(() => vi.unstubAllEnvs());

it("accepts a complete, well-formed build", async () => {
  expect(await problemsWith(good)).toEqual([]);
});

it("names a value pasted into the wrong setting, and a missing one", async () => {
  expect(
    await problemsWith({ ...good, VITE_FIREBASE_API_KEY: "web-staging.up.railway.app", VITE_FIREBASE_APP_ID: "" }),
  ).toEqual(["VITE_FIREBASE_API_KEY doesn't look right", "VITE_FIREBASE_APP_ID is not set"]);
});

it("rejects an API base with a path, which the CSP could not match", async () => {
  expect(await problemsWith({ ...good, VITE_API_BASE: "https://core-api.example.test/v1" })).toEqual([
    "VITE_API_BASE doesn't look right",
  ]);
});
