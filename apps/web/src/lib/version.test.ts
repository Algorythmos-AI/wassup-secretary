import { expect, it } from "vitest";
import { newerBuildAvailable } from "./version";

declare const __BUILD_ID__: string;
const serve = (body: unknown, status = 200) => (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;

it("asks for a reload only when the server has a different, real build", async () => {
  expect(await newerBuildAvailable(serve({ build: __BUILD_ID__ }))).toBe(false);
  expect(await newerBuildAvailable(serve({ build: "another-tree" }))).toBe(true);
  expect(await newerBuildAvailable(serve({ build: "" }))).toBe(false);
  expect(await newerBuildAvailable(serve({}, 502))).toBe(false); // mid-deploy
  expect(await newerBuildAvailable((async () => Promise.reject(new Error("offline"))) as typeof fetch)).toBe(false);
});
