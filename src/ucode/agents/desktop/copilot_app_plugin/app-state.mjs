import { readFile, rename, writeFile } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";

import { tryLock } from "./app-rows.mjs";

export const STATE_LOG_LIMIT = 50;
export const STATE_LOCK_WAIT_MS = 50;
export const STATE_LOCK_ATTEMPTS = 20;
// The CLI sends SIGKILL 5 s after SIGTERM.
export const TERMINATE_WAIT_MS = 3_500;

// A small JSON log in the extension's directory, the one place a user can find what the extension
// did: its stderr reaches no log. Every session's extension writes the same file, so a write holds a
// lock; an entry that can't get it in time is dropped rather than overwriting another's.
export function createStateLog(
  path,
  { limit = STATE_LOG_LIMIT, lock = tryLock, wait = delay, now = Date.now, attempts = STATE_LOCK_ATTEMPTS } = {},
) {
  let writing = Promise.resolve();
  async function acquire() {
    for (let attempt = 0; ; attempt++) {
      const release = lock(`${path}.lock`, now);
      if (release || attempt + 1 >= attempts) return release;
      await wait(STATE_LOCK_WAIT_MS);
    }
  }
  const record = (entry) => {
    writing = writing
      .then(async () => {
        const release = await acquire();
        if (!release) return;
        try {
          let entries = [];
          try {
            const parsed = JSON.parse(await readFile(path, "utf8"));
            if (Array.isArray(parsed)) entries = parsed;
          } catch {
            // A missing or unreadable log starts over.
          }
          const next = `${path}.${process.pid}.tmp`;
          await writeFile(next, `${JSON.stringify([...entries, entry].slice(-limit), null, 1)}\n`);
          await rename(next, path);
        } finally {
          release();
        }
      })
      .catch(() => {});
    return writing;
  };
  return record;
}

// The CLI stops an extension with SIGTERM, whose default would end the process with work still in
// flight. The steps run together within `ms`, and the signal is raised again afterwards, so the
// process still ends the way it would have.
export function shutdownBeforeTerminating(steps, proc = process, ms = TERMINATE_WAIT_MS) {
  proc.once("SIGTERM", () => {
    Promise.allSettled(steps.map((step) => Promise.resolve().then(() => step(ms)))).finally(() =>
      proc.kill(proc.pid, "SIGTERM"),
    );
  });
}
