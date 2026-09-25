import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { NotConfigured } from "../NotConfigured";

describe("NotConfigured", () => {
  it("names the missing settings for the administrator", () => {
    render(<NotConfigured missing={["VITE_FIREBASE_API_KEY"]} />);
    expect(screen.getByText(/hasn't been set up yet/)).toBeInTheDocument();
    expect(screen.getByText(/VITE_FIREBASE_API_KEY/)).toBeInTheDocument();
  });
});
