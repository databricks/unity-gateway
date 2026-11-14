// Offer the native effort vocabulary; Claude validates support when saving.
// Never infer per-model defaults or capabilities from a model's generation.
export const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];
type EffortValues = {effortLevel?: string; maxEffortLevel?: string};
export type EffortSettings = EffortValues & {modelSettings?: Record<string, EffortValues>};

export function effortProfile(id: string, layers: readonly (EffortSettings | null | undefined)[], environment?: string, status?: string) {
  const canonical = id.replace(/^ug-auto-[a-f0-9]{12}-/, '');
  let cap = EFFORT_LEVELS.length - 1;
  for (const layer of layers) {
    const max = layer?.modelSettings?.[id]?.maxEffortLevel
      ?? layer?.modelSettings?.[canonical]?.maxEffortLevel ?? layer?.maxEffortLevel;
    if (max && EFFORT_LEVELS.includes(max)) cap = Math.min(cap, EFFORT_LEVELS.indexOf(max));
  }
  const unsupported = status && /does not support|not supported/i.test(status);
  const levels = unsupported ? [] : EFFORT_LEVELS.slice(0, cap + 1);
  const native = status?.match(/currently (low|medium|high|xhigh|max)/i)?.[1]
    ?? status?.match(/effort level(?: to|:)\s*(low|medium|high|xhigh|max)/i)?.[1];
  const override = environment && environment !== 'unset' ? environment : undefined;
  // An unvisited model keeps its native default; selecting a row never invents one.
  const current = override ?? native ?? 'auto';
  const index = EFFORT_LEVELS.indexOf(current.toLowerCase());
  const level = !override && index >= 0 ? EFFORT_LEVELS[Math.min(index, cap)] : current;
  return {levels, level, locked: Boolean(override),
    note: override ? 'set by environment' : !native ? 'model default' : ''};
}
