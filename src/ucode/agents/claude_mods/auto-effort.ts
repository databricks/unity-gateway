// Capability table matches Claude's documented effort scale. Native /effort is
// still the authority at commit time (including server-side organization caps).
export const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];

export function effortProfile(id: string, layers: any[], environment?: string, status?: string) {
  const canonical = id.match(/claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)?/)?.[0] ?? id;
  const supported = /claude-fable-/.test(canonical)
    || /claude-(?:opus|sonnet)-5/.test(canonical)
    || canonical === 'claude-haiku-5-5'
    || /claude-opus-4-[5678]$/.test(canonical) || canonical === 'claude-sonnet-4-6';
  let levels = supported ? [...EFFORT_LEVELS] : [];
  if (/claude-(?:opus|sonnet)-4-[56]$/.test(canonical)) levels = levels.filter(v => v !== 'xhigh');
  let cap = EFFORT_LEVELS.length - 1;
  let saved: string | undefined;
  for (const layer of layers) {
    const perModel = layer?.modelSettings?.[canonical] ?? layer?.modelSettings?.[id];
    const max = perModel?.maxEffortLevel ?? layer?.maxEffortLevel;
    if (EFFORT_LEVELS.includes(max)) cap = Math.min(cap, EFFORT_LEVELS.indexOf(max));
    // Legacy user defaults don't apply to the newer 5.5 generation.
    const global = layer === layers[0] && /-5-5$/.test(canonical) ? undefined : layer?.effortLevel;
    if (perModel?.effortLevel ?? global) saved = perModel?.effortLevel ?? global;
  }
  levels = levels.filter(v => EFFORT_LEVELS.indexOf(v) <= cap);
  const fallback = /-5-5$|claude-fable-5-2$/.test(canonical) ? 'medium'
    : canonical === 'claude-opus-4-7' ? 'xhigh' : 'high';
  const native = status?.match(/currently (low|medium|high|xhigh|max)/i)?.[1]
    ?? status?.match(/effort level(?: to|:)\s*(low|medium|high|xhigh|max)/i)?.[1];
  let level = environment || native || saved || fallback;
  if (level === 'unset') level = 'auto';
  if (EFFORT_LEVELS.includes(level) && !levels.includes(level) && levels.length) {
    level = levels.filter(v => EFFORT_LEVELS.indexOf(v) <= EFFORT_LEVELS.indexOf(level)).at(-1) ?? levels[0];
  }
  return { levels, level, locked: Boolean(environment),
    note: environment ? 'set by environment' : !saved && (!status || /level: auto/i.test(status)) ? 'default' : '' };
}
