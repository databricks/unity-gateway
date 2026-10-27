import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { expiresAt, Tokens } from "../src/auth";
import { jwt, rejectsWith, tokenCommand } from "./support";

describe("Tokens", () => {
  it("reuses a token until it nears expiry", async () => {
    const valid = tokenCommand([jwt(3600)]);
    const tokens = new Tokens(valid.command);
    assert.equal(await tokens.get(), await tokens.get());
    assert.equal(valid.runs(), 1);

    const expiring = tokenCommand([jwt(60), jwt(3600)]);
    const refreshed = new Tokens(expiring.command);
    assert.notEqual(await refreshed.get(), await refreshed.get());
    assert.equal(expiring.runs(), 2);
  });

  it("runs the command once for concurrent requests", async () => {
    const source = tokenCommand([jwt(3600)]);
    const tokens = new Tokens(source.command);
    await Promise.all([tokens.get(), tokens.get(), tokens.get()]);
    assert.equal(source.runs(), 1);
  });

  it("forces a new token after a rejection, but not after a stale one", async () => {
    const source = tokenCommand(["fresh"], "cached");
    const tokens = new Tokens(source.command);
    assert.equal(await tokens.get(), "cached");
    assert.equal(await tokens.renew("cached"), "fresh");
    assert.equal(await tokens.renew("cached"), "fresh");
    assert.equal(source.runs(), 2);
  });

  it("waits for a fetch in flight before deciding to force one", async () => {
    const source = tokenCommand(["fresh"], "cached");
    const tokens = new Tokens(source.command);
    assert.deepEqual(await Promise.all([tokens.get(), tokens.renew("cached")]), ["cached", "fresh"]);
    assert.equal(source.runs(), 2);
  });

  it("reports the command's failure and empty output", async () => {
    await rejectsWith(new Tokens(`node -e "console.error('sign-in expired'); process.exit(1)"`).get(), "sign-in expired");
    await rejectsWith(new Tokens(`node -e ""`).get(), "printed nothing");
  });

  it("reads exp from a JWT and gives other tokens an hour", () => {
    assert.equal(expiresAt(`h.${Buffer.from('{"exp":1800000000}').toString("base64url")}.s`), 1_800_000_000_000);
    assert.ok(expiresAt("dapi123") - Date.now() > 59 * 60_000);
  });
});
