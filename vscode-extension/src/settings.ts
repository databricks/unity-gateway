import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type * as vscode from "vscode";

/** globalState key of the settings values the extension last wrote. */
export const OWNED_SETTINGS_KEY = "unityGateway.ownedSettings";

/**
 * A copy of the record in the extension's global storage folder. `ug` reads it before uninstalling the extension, which
 * can't clean up after itself, and removes those values from the user settings.
 */
export const OWNED_SETTINGS_FILE = "owned-settings.json";

/** VS Code's default model for new chat conversations, matched against each model's id. */
export const DEFAULT_MODEL_SETTING = "chat.defaultModel";

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Writes `wanted` to the user settings, and removes a value it wrote before that is no longer wanted. A value the
 * developer changed since is theirs and stays. A key in `developerWins` is only written while the developer hasn't
 * set a value of their own. Returns what the extension owns afterwards.
 */
export const syncSettings = async (
  settings: Pick<vscode.WorkspaceConfiguration, "inspect" | "update">,
  state: Pick<vscode.Memento, "get" | "update">,
  wanted: Readonly<Record<string, unknown>>,
  log: (line: string) => void,
  developerWins: ReadonlySet<string> = new Set(),
): Promise<Record<string, unknown>> => {
  const owned = state.get<Record<string, unknown>>(OWNED_SETTINGS_KEY) ?? {};
  const write = async (key: string, value: unknown) => {
    try {
      // 1 is ConfigurationTarget.Global: the user settings.
      await settings.update(key, value, 1);
    } catch (error) {
      // A setting VS Code doesn't know, such as an agent this version doesn't have.
      log(`Could not update ${key}: ${String(error)}`);
    }
  };
  for (const [key, value] of Object.entries(owned)) {
    if (!(key in wanted) && same(settings.inspect(key)?.globalValue, value)) {
      await write(key, undefined);
    }
  }
  const nowOwned: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(wanted)) {
    const current = settings.inspect(key)?.globalValue;
    const developers = current !== undefined && !(key in owned && same(current, owned[key]));
    if (developerWins.has(key) && developers) {
      continue;
    }
    if (!same(current, value)) {
      await write(key, value);
    }
    nowOwned[key] = value;
  }
  await state.update(OWNED_SETTINGS_KEY, Object.keys(nowOwned).length > 0 ? nowOwned : undefined);
  return nowOwned;
};

/** Writes `owned` to `folder`'s copy of the record, or removes the copy when the extension owns nothing. */
export const writeOwnedSettingsFile = (folder: string, owned: Readonly<Record<string, unknown>>): void => {
  const path = join(folder, OWNED_SETTINGS_FILE);
  if (Object.keys(owned).length === 0) {
    rmSync(path, { force: true });
    return;
  }
  mkdirSync(folder, { recursive: true });
  writeFileSync(path, JSON.stringify(owned, null, 2) + "\n");
};
