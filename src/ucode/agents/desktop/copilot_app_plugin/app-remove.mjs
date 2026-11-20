import { randomUUID } from "node:crypto";
import { chmodSync, lstatSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

import {
  LOCK_FILE,
  RESTART_MARKER_FILE,
  SYNC_STAMP_FILE,
  connectApp,
  copilotHome,
  deleteProviders,
  readEndpoint,
  tryLock,
} from "./app-rows.mjs";

export const MARKETPLACE = "databricks-unity-gateway";
export const PLUGIN_KEY = "unity-gateway@databricks-unity-gateway";
// Earlier builds' ids, which a removal takes out too.
const IDS = [
  [MARKETPLACE, PLUGIN_KEY],
  ["unity-gateway", "databricks@unity-gateway"],
];
const MARKETPLACES_KEY = "extraKnownMarketplaces";
const ENABLED_KEY = "enabledPlugins";

export class SettingsError extends Error {}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

export function settingsPath(home = copilotHome()) {
  return join(home, "settings.json");
}

// Copilot ignores a settings file that isn't a JSON object, so one is never rewritten.
export function readSettings(path) {
  let stat;
  try {
    stat = lstatSync(path);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  if (stat.isSymbolicLink()) {
    throw new SettingsError(`${path} is a symlink, which ug won't replace. Edit the file it points to instead.`);
  }
  let settings;
  try {
    settings = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    throw new SettingsError(`${path} is not valid JSON`);
  }
  if (!isObject(settings)) throw new SettingsError(`${path} is not a JSON object`);
  return { settings, mode: stat.mode & 0o777 };
}

// A marketplace of ug's name at another path isn't ug's, so it and its plugin key stay.
function ugKeys(settings, pluginRoot) {
  const marketplaces = settings[MARKETPLACES_KEY];
  return IDS.flatMap(([marketplace, pluginKey]) => {
    if (isObject(marketplaces) && Object.hasOwn(marketplaces, marketplace)) {
      const source = marketplaces[marketplace]?.source;
      if (!isObject(source) || source.path !== pluginRoot) return [];
    }
    return [
      [MARKETPLACES_KEY, marketplace],
      [ENABLED_KEY, pluginKey],
    ].filter(([key, entry]) => isObject(settings[key]) && Object.hasOwn(settings[key], entry));
  });
}

export function hasUgKeys(settings, pluginRoot) {
  return ugKeys(settings, pluginRoot).length > 0;
}

function writeSettings(path, settings, mode) {
  const temp = join(dirname(path), `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temp, JSON.stringify(settings, null, 2), { flag: "wx", mode });
    chmodSync(temp, mode);
    renameSync(temp, path);
  } catch (error) {
    try {
      unlinkSync(temp);
    } catch {}
    throw error;
  }
}

export function removeSettingsKeys(path, pluginRoot) {
  const current = readSettings(path);
  if (!current) return false;
  const { settings, mode } = current;
  const keys = ugKeys(settings, pluginRoot);
  if (!keys.length) return false;
  for (const [key, entry] of keys) {
    delete settings[key][entry];
    if (!Object.keys(settings[key]).length) delete settings[key];
  }
  writeSettings(path, settings, mode);
  return true;
}

// The keys go only once the rows are gone: without the plugin, no later start could delete them.
export async function removeOnce(config, { home = copilotHome(), stateDir, now = Date.now, ...connect } = {}) {
  if (!config.remove) return "skipped";
  const path = settingsPath(home);
  const release = tryLock(join(stateDir, LOCK_FILE), now);
  if (!release) return "busy";
  try {
    let current;
    let refusal = null;
    try {
      current = readSettings(path);
    } catch (error) {
      if (!(error instanceof SettingsError)) throw error;
      refusal = error;
    }
    if (!refusal && !(current && hasUgKeys(current.settings, config.pluginRoot))) return "done";
    const providerIds = config.app?.ownedProviderIds ?? [];
    if (providerIds.length) {
      const endpoint = readEndpoint(home);
      if (!endpoint) return "not_running";
      const app = await connectApp(endpoint, connect);
      try {
        await deleteProviders(app, providerIds);
      } finally {
        app.close();
      }
      // A later install must write the rows and restart the app again, not match the last sync.
      for (const name of [SYNC_STAMP_FILE, RESTART_MARKER_FILE]) {
        try {
          unlinkSync(join(stateDir, name));
        } catch {}
      }
    }
    if (refusal) throw refusal;
    removeSettingsKeys(path, config.pluginRoot);
    return "removed";
  } finally {
    release();
  }
}

export async function removeInBackground(config, options, log) {
  try {
    return await removeOnce(config, options);
  } catch (error) {
    try {
      log(`Databricks models: couldn't remove Unity Gateway from the GitHub Copilot app: ${error?.message ?? error}`);
    } catch {}
    return error instanceof SettingsError ? "refused" : "failed";
  }
}
