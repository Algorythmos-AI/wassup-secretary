import { expect, it } from "vitest";
import { coalesce } from "./coalesce";

it("collapses any number of triggers during a run into one follow-up run", async () => {
  let runs = 0;
  let release: () => void = () => {};
  const task = coalesce(async () => {
    runs += 1;
    if (runs === 1) await new Promise<void>((r) => (release = r));
  });
  const first = task();
  for (let i = 0; i < 500; i++) void task();
  release();
  await first;
  expect(runs).toBe(2);
  await task();
  expect(runs).toBe(3);
});

it("keeps working after a failed run", async () => {
  let runs = 0;
  const task = coalesce(async () => {
    runs += 1;
    if (runs === 1) throw new Error("network");
  });
  await task();
  await task();
  expect(runs).toBe(2);
});
