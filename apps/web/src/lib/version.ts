/**
 * Wall screens run for weeks without anyone reloading them. Every few minutes this compares the
 * build the page is running with the one the server now serves (/version.json, never cached)
 * and reloads when they differ, so a deploy (including a fix) reaches every office TV.
 */
import { useEffect } from "react";

declare const __BUILD_ID__: string;
const CHECK_EVERY_MS = 5 * 60_000;

export async function newerBuildAvailable(fetchImpl: typeof fetch = fetch): Promise<boolean> {
  try {
    const response = await fetchImpl("/version.json", { cache: "no-store" });
    if (!response.ok) return false;
    const { build } = (await response.json()) as { build?: string };
    return typeof build === "string" && build !== "" && build !== __BUILD_ID__;
  } catch {
    return false; // offline or mid-deploy: try again next time
  }
}

export function useReloadOnNewVersion(): void {
  useEffect(() => {
    const timer = window.setInterval(() => {
      void newerBuildAvailable().then((newer) => newer && window.location.reload());
    }, CHECK_EVERY_MS);
    return () => window.clearInterval(timer);
  }, []);
}
