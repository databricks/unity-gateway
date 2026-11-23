import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import {
  STATE_LOCK_ATTEMPTS,
  createStateLog,
  shutdownBeforeTerminating,
} from "../src/ucode/agents/desktop/copilot_app_plugin/app-state.mjs";

test("the state log keeps the last entries and survives a broken file", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "ug-state-log-")), "state.json");
  const record = createStateLog(path, { limit: 3 });

  await record({ n: 1 });
  for (let n = 2; n <= 5; n++) record({ n });
  await record({ n: 6 });
  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), [{ n: 4 }, { n: 5 }, { n: 6 }]);

  const broken = createStateLog(path, { limit: 3 });
  writeFileSync(path, "not json");
  await broken({ n: 7 });
  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), [{ n: 7 }]);
});

test("state logs of two extension processes don't lose each other's entries", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ug-state-log-"));
  const path = join(dir, "state.json");
  const first = createStateLog(path);
  const second = createStateLog(path);

  await Promise.all([first({ n: 1 }), second({ n: 2 })]);

  assert.deepEqual(
    JSON.parse(readFileSync(path, "utf8")).sort((a, b) => a.n - b.n),
    [{ n: 1 }, { n: 2 }],
  );
  assert.deepEqual(readdirSync(dir), ["state.json"]);
});

test("an entry that can't get the state log's lock is dropped, not written over another", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "ug-state-log-")), "state.json");
  writeFileSync(path, JSON.stringify([{ n: 1 }]));
  const waits = [];
  const record = createStateLog(path, { lock: () => null, wait: async (ms) => waits.push(ms) });

  await record({ n: 2 });

  assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), [{ n: 1 }]);
  assert.equal(waits.length, STATE_LOCK_ATTEMPTS - 1);
});

test("shutdownBeforeTerminating runs every step together, then raises SIGTERM again", async () => {
  const proc = new EventEmitter();
  proc.pid = 4242;
  const killed = [];
  proc.kill = (pid, signal) => killed.push([pid, signal]);
  const order = [];
  shutdownBeforeTerminating(
    [
      async (ms) => {
        order.push(["a", ms]);
        await delay(20);
        order.push("a done");
      },
      async (ms) => {
        order.push(["b", ms]);
        throw new Error("step failed");
      },
    ],
    proc,
    500,
  );
  proc.emit("SIGTERM");
  for (let i = 0; i < 50 && killed.length === 0; i++) await delay(5);

  assert.deepEqual(order, [["a", 500], ["b", 500], "a done"]);
  assert.deepEqual(killed, [[4242, "SIGTERM"]]);
});
