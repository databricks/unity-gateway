import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { OWNED_SETTINGS_FILE, OWNED_SETTINGS_KEY, syncSettings, writeOwnedSettingsFile } from "../src/settings";

describe("syncSettings", () => {
  const fake = (global: Record<string, unknown>, owned?: Record<string, unknown>, failing: string[] = []) => {
    const updates: [string, unknown][] = [];
    let saved: unknown = owned;
    return {
      updates,
      saved: () => saved,
      settings: {
        inspect: (key: string) => ({ key, globalValue: global[key] }),
        update: async (key: string, value: unknown) => {
          if (failing.includes(key)) {
            throw new Error(`${key} is not a registered configuration`);
          }
          updates.push([key, value]);
        },
      },
      state: {
        get: <T>() => saved as T,
        update: async (_key: string, value: unknown) => {
          saved = value;
        },
      },
    };
  };

  it("writes what differs, records it, and logs a setting VS Code doesn't have", async () => {
    const store = fake({ a: 1, b: 2 }, undefined, ["c"]);
    const logs: string[] = [];

    const owned = await syncSettings(store.settings as never, store.state as never, { a: 1, b: 3, c: 4 }, (line) => logs.push(line));

    assert.deepEqual(store.updates, [["b", 3]]);
    assert.deepEqual(store.saved(), { a: 1, b: 3, c: 4 });
    assert.deepEqual(owned, { a: 1, b: 3, c: 4 });
    assert.ok(logs[0]?.includes("Could not update c"));
  });

  it("removes what it wrote once no longer wanted, unless the developer changed it", async () => {
    const store = fake({ a: 1, b: "mine" }, { a: 1, b: 2 });

    await syncSettings(store.settings as never, store.state as never, {}, () => {});

    assert.deepEqual(store.updates, [["a", undefined]]);
    assert.equal(store.saved(), undefined);
    assert.equal(OWNED_SETTINGS_KEY, "unityGateway.ownedSettings");
  });

  it("leaves a developer's own value of a developer-wins key, and keeps writing its own or an unset one", async () => {
    const theirs = fake({ "chat.defaultModel": "gpt-mine" });
    await syncSettings(theirs.settings as never, theirs.state as never, { "chat.defaultModel": "system.ai.opus" }, () => {}, new Set(["chat.defaultModel"]));
    assert.deepEqual(theirs.updates, []);
    assert.equal(theirs.saved(), undefined);

    // The developer picked the managed default themselves: it stays theirs when the default changes or goes.
    const same = fake({ "chat.defaultModel": "system.ai.opus" });
    for (const wanted of [{ "chat.defaultModel": "system.ai.opus" }, { "chat.defaultModel": "system.ai.sonnet" }, {}]) {
      await syncSettings(same.settings as never, same.state as never, wanted, () => {}, new Set(["chat.defaultModel"]));
    }
    assert.deepEqual(same.updates, []);
    assert.equal(same.saved(), undefined);

    const ours = fake({ "chat.defaultModel": "system.ai.sonnet" }, { "chat.defaultModel": "system.ai.sonnet" });
    await syncSettings(ours.settings as never, ours.state as never, { "chat.defaultModel": "system.ai.opus" }, () => {}, new Set(["chat.defaultModel"]));
    assert.deepEqual(ours.updates, [["chat.defaultModel", "system.ai.opus"]]);
    assert.deepEqual(ours.saved(), { "chat.defaultModel": "system.ai.opus" });

    const unset = fake({});
    await syncSettings(unset.settings as never, unset.state as never, { "chat.defaultModel": "system.ai.opus" }, () => {}, new Set(["chat.defaultModel"]));
    assert.deepEqual(unset.updates, [["chat.defaultModel", "system.ai.opus"]]);
  });
});

describe("writeOwnedSettingsFile", () => {
  it("keeps a copy of the record for ug, and removes it once the extension owns nothing", () => {
    const folder = join(mkdtempSync(join(tmpdir(), "ug-owned-")), "databricks.unity-gateway");
    const path = join(folder, OWNED_SETTINGS_FILE);

    writeOwnedSettingsFile(folder, { "chat.defaultModel": "system.ai.opus" });
    assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), { "chat.defaultModel": "system.ai.opus" });

    writeOwnedSettingsFile(folder, {});
    assert.equal(existsSync(path), false);
    writeOwnedSettingsFile(folder, {});
  });
});
