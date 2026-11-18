import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { bundledRuntime, RUNTIME_WRAPPER_NAME, seatRelaySetting, wrapperScript, writeWrapper } from "../src/seatRelay";

describe("seatRelaySetting", () => {
  it("keeps a command of non-empty strings and drops anything else", () => {
    assert.deepEqual(seatRelaySetting({ command: ["/bin/ug", "copilot-seat-runtime"] }), {
      command: ["/bin/ug", "copilot-seat-runtime"],
    });
    for (const value of [undefined, null, "ug", {}, { command: [] }, { command: ["ug", ""] }, { command: ["ug", 1] }]) {
      assert.equal(seatRelaySetting(value), undefined);
    }
  });
});

describe("bundledRuntime", () => {
  it("names the runtime VS Code bundles for the platform", () => {
    assert.equal(
      bundledRuntime("/opt/code/resources/app", "linux", "x64"),
      "/opt/code/resources/app/node_modules.asar.unpacked/@github/copilot-sdk-linux-x64/prebuilds/linux-x64/copilot-runtime",
    );
    assert.ok(bundledRuntime("/a", "darwin", "arm64").endsWith("copilot-sdk-darwin-arm64/prebuilds/darwin-arm64/copilot-runtime"));
  });
});

describe("wrapperScript", () => {
  it("runs the ug command on the runtime with VS Code's arguments, quoted for the shell", () => {
    const dir = mkdtempSync(join(tmpdir(), "seat relay "));
    const record = join(dir, "it's args");
    const fakeUg = join(dir, "fake ug");
    writeFileSync(fakeUg, `#!/bin/sh\nfor arg in "$@"; do printf '%s\\n' "$arg"; done > "$1"\n`, { mode: 0o700 });
    const wrapper = join(dir, RUNTIME_WRAPPER_NAME);
    writeFileSync(wrapper, wrapperScript([fakeUg, record, "copilot-seat-runtime"], "/run time/copilot-runtime"), { mode: 0o700 });

    execFileSync(wrapper, ["--stdio", "a b", "$HOME", "'q'"]);

    assert.deepEqual(readFileSync(record, "utf8").trimEnd().split("\n"), [
      record,
      "copilot-seat-runtime",
      "--runtime",
      "/run time/copilot-runtime",
      "--",
      "--stdio",
      "a b",
      "$HOME",
      "'q'",
    ]);
  });
});

describe("writeWrapper", () => {
  const setting = { command: ["/bin/ug", "copilot-seat-runtime", "--provider", "main.ai.seats"] };
  const appWithRuntime = () => {
    const appRoot = mkdtempSync(join(tmpdir(), "app-"));
    const runtime = bundledRuntime(appRoot, "linux", "x64");
    mkdirSync(dirname(runtime), { recursive: true });
    writeFileSync(runtime, "");
    return { appRoot, runtime };
  };

  it("writes an executable wrapper for this VS Code's runtime", async () => {
    const { appRoot, runtime } = appWithRuntime();
    const dir = join(mkdtempSync(join(tmpdir(), "storage-")), "missing");

    const path = await writeWrapper(dir, setting, appRoot, () => {}, "linux", "x64");

    assert.equal(path, join(dir, RUNTIME_WRAPPER_NAME));
    assert.equal(readFileSync(path, "utf8"), wrapperScript(setting.command, runtime));
    assert.equal(statSync(path).mode & 0o777, 0o700);
  });

  it("writes nothing when VS Code bundles no runtime, or on Windows", async () => {
    const logs: string[] = [];
    const { appRoot } = appWithRuntime();
    const dir = mkdtempSync(join(tmpdir(), "storage-"));

    assert.equal(await writeWrapper(dir, setting, appRoot, (line) => logs.push(line), "linux", "arm64"), undefined);
    assert.equal(await writeWrapper(dir, setting, appRoot, (line) => logs.push(line), "win32", "x64"), undefined);
    assert.equal(logs.length, 2);
    assert.ok(logs[0]?.includes("no Copilot runtime"));
    assert.ok(logs[1]?.includes("Windows"));
  });
});
