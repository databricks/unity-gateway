import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type * as vscode from "vscode";
import { LanguageModelDataPart, LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart } from "./vscode";

export const SYSTEM = 3;
export const USER = 1;
export const ASSISTANT = 2;

export const message = (role: number, ...content: unknown[]) => ({ role, content, name: undefined }) as unknown as vscode.LanguageModelChatRequestMessage;
export const text = (value: string) => new LanguageModelTextPart(value);
export const image = (byte: number) => new LanguageModelDataPart(new Uint8Array([byte]), "image/png");
export const call = (id: string, name: string, input: object) => new LanguageModelToolCallPart(id, name, input);
export const result = (id: string, ...content: unknown[]) => new LanguageModelToolResultPart(id, content);
export const weatherTool: vscode.LanguageModelChatTool = {
  name: "get_weather",
  description: "Weather for a city",
  inputSchema: { type: "object", properties: { city: { type: "string" } } },
};

export const jwt = (expiresInSeconds: number) =>
  `h.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + expiresInSeconds })).toString("base64url")}.s`;

/**
 * A real shell command that prints the next of `tokens` on each run and counts its runs. With `cached`, it
 * prints `cached` until it's run with --force-refresh, like the Databricks CLI's token cache.
 */
export const tokenCommand = (tokens: string[], cached?: string) => {
  const dir = mkdtempSync(join(tmpdir(), "ug-vscode-test-"));
  const state = join(dir, "runs");
  writeFileSync(state, "0");
  const script = join(dir, "token.js");
  writeFileSync(
    script,
    `const fs = require("fs"); const n = Number(fs.readFileSync(${JSON.stringify(state)}, "utf8")); fs.writeFileSync(${JSON.stringify(state)}, String(n + 1));
     const tokens = ${JSON.stringify(tokens)}; const cached = ${JSON.stringify(cached ?? null)};
     const token = cached !== null && !process.argv.includes("--force-refresh") ? cached : tokens[Math.min(n, tokens.length - 1)];
     process.stdout.write(token + "\\n");`,
  );
  return { command: `node ${JSON.stringify(script)}`, runs: () => Number(readFileSync(state, "utf8")) };
};

export async function* events(...items: unknown[]): AsyncGenerator<unknown> {
  yield* items;
}

export const collect = async <T>(items: AsyncIterable<T>): Promise<T[]> => {
  const out: T[] = [];
  for await (const item of items) {
    out.push(item);
  }
  return out;
};
