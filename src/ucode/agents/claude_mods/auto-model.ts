export type AutoPicker = {
  model: string;
  mainModel: string;
  storeKey: string;
  description: string;
  models: { model: string; label: string; autoModel: string }[];
  // Static-catalog mode: the immutable picker row's alias. Native keeps this
  // alias selected, so /model checks Auto for every main model, and `variant`
  // carries the session's main model instead of a per-model alias.
  staticModel?: string;
  // Start a static-catalog session on the saved main model (no launch-only model).
  preferSaved?: boolean;
  variant?: string;
};

export function isStaticAuto(auto: AutoPicker, model: string): boolean {
  return Boolean(auto.staticModel) && matchesAutoModel(auto.staticModel!, model);
}

export function matchesAutoModel(configured: string, selected: string): boolean {
  // Native 2.1.323 strips the context suffix from selected Auto aliases.
  return selected === configured || (configured.endsWith('[1m]') && selected === configured.slice(0, -4));
}

export function resolveAutoModel(auto: AutoPicker, selected: string): string {
  return auto.models.find(row => matchesAutoModel(row.autoModel, selected))?.autoModel ?? selected;
}

// The real model an Auto alias stands for, or undefined for any other model.
// The static row stands for the session's main model.
export function autoTarget(auto: AutoPicker, model: string): string | undefined {
  if (isStaticAuto(auto, model) && auto.variant) return auto.variant;
  return auto.models.find(row => matchesAutoModel(row.autoModel, model))?.model;
}

export function sameAutoTarget(auto: AutoPicker, from: string, to: string, overrides: unknown, window?: number): boolean {
  from = resolveAutoModel(auto, from);
  to = resolveAutoModel(auto, to);
  const row = auto.models.find(row => row.autoModel === to);
  if (!row || !overrides || typeof overrides !== 'object' || Array.isArray(overrides)) return false;
  const targets = new Map<string, unknown>(Object.entries(overrides));
  const target = targets.get(to);
  const current = targets.has(from) ? targets.get(from) : from;
  if (target !== row.model || (targets.has(row.model) && targets.get(row.model) !== row.model)) return false;
  const context = (model: string) => model.match(/\[[^\]]+\]$/)?.[0] ?? '';
  if (current === target && context(from) === context(row.model) && context(to) === context(row.model)) return true;
  // Native 2.1.290 gives these bare IDs a 1M default. Its picker adds [1m].
  const million = /^(?:system\.ai\.)?(claude-(?:opus|sonnet)-5-5)$/.exec(row.model);
  return Boolean(million && to.endsWith('-' + million[1]) && current === row.model + '[1m]'
    && context(from) === '[1m]' && window === 1_000_000);
}

const MODEL_CONFIRMATION = /^(<local-command-stdout>(?:Set model to|Kept model as) )(`+).+?\2([^\n<]*)/;

export const AUTO_MAIN_TITLE = 'Auto mode: Choose your main model';
export const AUTO_CONFIRMATION = 'Auto mode: Subagents smart routed.';

// Native confirms Auto before the chooser saves the main model.
export function formatAutoConfirmation(text: string): string {
  return text.replace(MODEL_CONFIRMATION, (_match, _before, _quotes, details) => {
    const suffix = details.trim().replace(/^and /, '')
      .replace(/^saved as your default for new sessions\b/, '').trim();
    return `<local-command-stdout>${AUTO_CONFIRMATION}`
      + (suffix ? `\n${suffix[0].toUpperCase()}${suffix.slice(1)}` : '');
  });
}
