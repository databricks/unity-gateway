import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { windowsTool } from "../src/ucode/agents/desktop/copilot_app_plugin/app-windows.mjs";

test("windowsTool runs the system folder's copy when SystemRoot has it, else the bare name", () => {
  const root = mkdtempSync(join(tmpdir(), "ug-systemroot-"));
  mkdirSync(join(root, "System32", "WindowsPowerShell", "v1.0"), { recursive: true });
  writeFileSync(join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), "");
  writeFileSync(join(root, "System32", "reg.exe"), "");
  assert.equal(windowsTool("powershell.exe", { env: { SystemRoot: root } }), join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"));
  assert.equal(windowsTool("reg.exe", { env: { SYSTEMROOT: root } }), join(root, "System32", "reg.exe"));

  const empty = mkdtempSync(join(tmpdir(), "ug-systemroot-empty-"));
  assert.equal(windowsTool("powershell.exe", { env: { SystemRoot: empty } }), "powershell.exe");
  assert.equal(windowsTool("reg.exe", { env: { SystemRoot: empty } }), "reg.exe");
});

test("windowsTool defaults the root to C:\\Windows and leaves unknown tools alone", () => {
  const seen = [];
  const exists = (path) => (seen.push(path), true);
  assert.equal(windowsTool("reg.exe", { env: {}, exists }), join("C:\\Windows", "System32", "reg.exe"));
  assert.equal(seen.length, 1);
  assert.equal(windowsTool("scutil", { env: {}, exists }), "scutil");
});
