import { describe, expect, it } from "vitest";
import { googleProvider } from "../auth";

describe("googleProvider", () => {
  it("always asks Google to show the account picker", () => {
    // Without it a shared computer keeps signing in as whoever used it last.
    expect(googleProvider().getCustomParameters()).toEqual({ prompt: "select_account" });
  });
});
