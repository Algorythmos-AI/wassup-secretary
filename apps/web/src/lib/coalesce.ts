/**
 * Run `task` now, or, if a run is already going, once more after it: any number of triggers
 * during a run collapse into a single follow-up. A burst of live events (say, a laptop waking up
 * and replaying a few hundred) costs two requests, not hundreds.
 */
export function coalesce(task: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null;
  let again = false;
  return () => {
    if (running) {
      again = true;
      return running;
    }
    running = (async () => {
      try {
        do {
          again = false;
          await task().catch(() => undefined);
        } while (again);
      } finally {
        running = null;
      }
    })();
    return running;
  };
}
