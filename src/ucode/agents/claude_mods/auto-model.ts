export type AutoPicker = {
  model: string;
  mainModel: string;
  storeKey: string;
  description: string;
  models: { model: string; label: string; autoModel: string }[];
};

export function sameAutoTarget(auto: AutoPicker, from: string, to: string, overrides: unknown, window?: number): boolean {
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

export function formatAutoConfirmation(text: string, mainModel: string, setModel: boolean): string {
  return text.replace(MODEL_CONFIRMATION, (_match, _before, _quotes, details) => {
    const model = mainModel.replace(/^system\.ai\./, '');
    const suffix = details.trim().replace(/^and /, '')
      .replace(/^saved as your default for new sessions\b/, '').trim();
    return `<local-command-stdout>${setModel ? 'Set to' : 'Kept'} Auto mode with main model \`${model}\`. Subagents are smart routed.`
      + (suffix ? `\n${suffix[0].toUpperCase()}${suffix.slice(1)}` : '');
  });
}
