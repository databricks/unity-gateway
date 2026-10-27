import { describe, expect, it } from "vitest";
import { expiresAt, Tokens } from "../src/auth";
import { jwt, tokenCommand } from "./support";

describe("Tokens", () => {
  it("reuses a token until it nears expiry", async () => {
    const valid = tokenCommand([jwt(3600)]);
    const tokens = new Tokens(valid.command);
    expect(await tokens.get()).toBe(await tokens.get());
    expect(valid.runs()).toBe(1);

    const expiring = tokenCommand([jwt(60), jwt(3600)]);
    const refreshed = new Tokens(expiring.command);
    expect(await refreshed.get()).not.toBe(await refreshed.get());
    expect(expiring.runs()).toBe(2);
  });

  it("runs the command once for concurrent requests", async () => {
    const source = tokenCommand([jwt(3600)]);
    const tokens = new Tokens(source.command);
    await Promise.all([tokens.get(), tokens.get(), tokens.get()]);
    expect(source.runs()).toBe(1);
  });

  it("forces a new token after a rejection, but not after a stale one", async () => {
    const source = tokenCommand(["fresh"], "cached");
    const tokens = new Tokens(source.command);
    expect(await tokens.get()).toBe("cached");
    expect(await tokens.renew("cached")).toBe("fresh");
    expect(await tokens.renew("cached")).toBe("fresh");
    expect(source.runs()).toBe(2);
  });

  it("waits for a fetch in flight before deciding to force one", async () => {
    const source = tokenCommand(["fresh"], "cached");
    const tokens = new Tokens(source.command);
    const [first, renewed] = await Promise.all([tokens.get(), tokens.renew("cached")]);
    expect([first, renewed]).toEqual(["cached", "fresh"]);
    expect(source.runs()).toBe(2);
  });

  it("reports the command's failure and empty output", async () => {
    await expect(new Tokens(`node -e "console.error('sign-in expired'); process.exit(1)"`).get()).rejects.toThrow("sign-in expired");
    await expect(new Tokens(`node -e ""`).get()).rejects.toThrow("printed nothing");
  });

  it("reads exp from a JWT and gives other tokens an hour", () => {
    expect(expiresAt(`h.${Buffer.from('{"exp":1800000000}').toString("base64url")}.s`)).toBe(1_800_000_000_000);
    expect(expiresAt("dapi123") - Date.now()).toBeGreaterThan(59 * 60_000);
  });
});
