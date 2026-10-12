import type { On, PluginOptions } from 'claude-code';

// Smart-routing savings estimate, drawn above the prompt with the Smart Router
// Orchestrator skill version. Per turn request (main agent and subagents) it sums
// token usage by (agent, baseline, served, before_user_switch), where served is
// the model the request named (the model service billed; the id the API reports
// back is a gateway deployment's, which drifts) and baseline is what the request
// would have used without routing: the main model in effect for a subagent, served
// itself for main (routing never changes the main model mid-session). First-prompt
// routing switches the main model once, before the first request, so a later
// change is the user's; requests before it are flagged, and priced from
// start_model under that routing.
// Claude Code's side queries (title, compaction, helpers) are not turn steps, so
// they never count. A hooks module has no Node APIs and the price table lives in
// Python, so after each turn it writes the sums to the launch's savings_usage
// file and runs the savings_pricer command (ucode.smart_routing.savings) on it.
// Everything is best effort and swallows errors.

const USAGE_VERSION = 1;
const PRICER_TIMEOUT_MS = 20_000;
const TOKEN_KEYS = [
  'input_tokens',
  'output_tokens',
  'cache_creation_input_tokens',
  'cache_read_input_tokens',
] as const;
const CACHE_SPLIT_KEYS = ['ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'] as const;

type Totals = Record<(typeof TOKEN_KEYS)[number], number> & {
  cache_creation?: Partial<Record<(typeof CACHE_SPLIT_KEYS)[number], number>>;
};
type Entry = {
  agent: string;
  baseline: string;
  served: string;
  before_user_switch: boolean;
  usage: Totals;
};

const num = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);

// Set from the launch's plugin options by registerSavings.
let pricer: string[] = [];
let usagePath: string | null = null;
let enabled = false;
let version: string | null = null;

const entries = new Map<string, Entry>();
// The model the session started on, before any first-prompt routing. Set once.
let startModel: string | null = null;
// The model the main loop last named, so a subagent's baseline is a real model
// id ($.session.model() may return an alias).
let lastMainModel: string | null = null;
// The model the main loop's first request named (first-prompt routing's pick).
let firstMainModel: string | null = null;
// Whether a main request has since named another model, which only the user does.
let userSwitched = false;
let seeding: Promise<void> | undefined;
let estimate: string | null = null;
let dirty = false;
let running = false;
let rerun = false;

async function mainModel($: any): Promise<string | null> {
  try {
    return str(await $.session.model());
  } catch {
    return null;
  }
}

function accumulate(
  agent: string,
  baseline: string,
  served: string,
  beforeUserSwitch: boolean,
  usage: any,
): void {
  const key = JSON.stringify([agent, baseline, served, beforeUserSwitch]);
  let entry = entries.get(key);
  if (!entry) {
    entry = {
      agent,
      baseline,
      served,
      before_user_switch: beforeUserSwitch,
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
      },
    };
    entries.set(key, entry);
  }
  for (const k of TOKEN_KEYS) entry.usage[k] += num(usage[k]);
  // The 5m/1h cache-write split is only summed where the API reported it.
  const split = usage.cache_creation;
  if (split && typeof split === 'object') {
    const sums = entry.usage.cache_creation ?? {};
    for (const k of CACHE_SPLIT_KEYS) if (k in split) sums[k] = (sums[k] ?? 0) + num(split[k]);
    entry.usage.cache_creation = sums;
  }
}

// A mod reload resets module state while the session carries on, so the first
// hook of a fresh module instance picks up where the last one left off from the
// file it wrote. A missing or malformed file just means starting empty.
async function loadUsage($: any): Promise<void> {
  try {
    const saved = JSON.parse(await $.fs.read(usagePath));
    if (!saved || saved.version !== USAGE_VERSION || !Array.isArray(saved.entries)) return;
    for (const item of saved.entries) {
      const agent = str(item?.agent);
      const baseline = str(item?.baseline);
      const served = str(item?.served);
      if (agent && baseline && served && item.usage && typeof item.usage === 'object') {
        accumulate(agent, baseline, served, item.before_user_switch === true, item.usage);
        dirty = true;
      }
    }
    startModel ??= str(saved.start_model);
    lastMainModel ??= str(saved.main_model);
    firstMainModel ??= str(saved.first_main_model);
    userSwitched ||= saved.user_switched === true;
  } catch {}
}

// State is seeded once per module instance, by whichever hook runs first;
// concurrent hooks wait on the same load.
async function ready($: any): Promise<boolean> {
  if (!enabled) return false;
  seeding ??= loadUsage($);
  await seeding;
  return true;
}

// The pricer's last stdout line; a failed run drops the estimate, which would be stale.
async function runPricer($: any): Promise<string | null> {
  try {
    const run = await $.process.run([...pricer, '--usage', usagePath], {
      timeoutMs: PRICER_TIMEOUT_MS,
    });
    if (run?.exitCode === 0) {
      const out = JSON.parse(String(run.stdout ?? '').trim().split('\n').pop() ?? '');
      if (out && typeof out === 'object') return str(out.savings);
    }
  } catch {}
  return null;
}

async function writeAndPrice($: any): Promise<void> {
  dirty = false;
  try {
    await $.fs.write(
      usagePath,
      JSON.stringify({
        version: USAGE_VERSION,
        start_model: startModel,
        main_model: lastMainModel,
        first_main_model: firstMainModel,
        user_switched: userSwitched,
        entries: Array.from(entries.values()),
      }),
    );
  } catch (error) {
    // Retry at the next turn.complete rather than pricing a file that is not there.
    dirty = true;
    throw error;
  }
  const next = await runPricer($);
  if (next !== estimate) {
    estimate = next;
    // Claude Code only redraws the band when its props change, not when module state does.
    $.ui.invalidate('ui.render');
  }
}

// One pricer run at a time; a request that arrives mid-run just asks for another.
async function flush($: any): Promise<void> {
  if (running) {
    rerun = true;
    return;
  }
  running = true;
  try {
    do {
      rerun = false;
      await writeAndPrice($);
    } while (rerun);
  } catch {
  } finally {
    running = false;
  }
}

const line = (): string | null =>
  [estimate, version && `Smart Router Orchestrator v${version}`].filter(Boolean).join(' · ') ||
  null;

// The module's session.start hook, which register.ts installs (a second unmatched
// hook on the same event fails the module load). It runs before the first prompt,
// so the start model is captured before any routing happens. session.start can
// fire again (/clear), by when first-prompt routing may have switched the model,
// so the start model is only ever set once. The flush is not awaited: it spawns a
// process and Claude Code waits on this hook before the first prompt.
export async function savingsSessionStart($: any, e: any, next: any): Promise<any> {
  if (await ready($)) {
    startModel ??= await mainModel($);
    void flush($);
  }
  return next(e);
}

export function registerSavings(on: On, options: PluginOptions): void {
  const raw = options as Record<string, unknown>;
  pricer =
    Array.isArray(raw.savings_pricer) && raw.savings_pricer.every(arg => typeof arg === 'string')
      ? (raw.savings_pricer as string[])
      : [];
  usagePath = str(raw.savings_usage);
  enabled = pricer.length > 0 && usagePath !== null;
  version = str(raw.orchestrator_version);

  // Fires for every request to a model, subagents included (e.agentId is set).
  // yield* forwards the streamed response untouched; only the finished result's
  // usage is read.
  on('turn.step', async function* ($, e, next) {
    const request = e as any;
    const agent = str(request.agentId);
    const active = await ready($);
    let baseline: string | null = null;
    if (active) {
      if (agent === null) {
        lastMainModel = str(request.model) ?? lastMainModel;
        firstMainModel ??= lastMainModel;
        userSwitched ||= lastMainModel !== firstMainModel;
      } else baseline = lastMainModel ?? (await mainModel($));
    }
    // Read before the request runs: a concurrent main request may switch it.
    const beforeUserSwitch = !userSwitched;
    const result = yield* next(e);
    try {
      const usage = (result as any)?.usage;
      const served = str(request.model) ?? str(usage?.model);
      // Main is its own baseline: routing leaves the main model alone.
      const base = agent === null ? served : baseline;
      if (active && usage && served && base) {
        accumulate(agent ?? 'main', base, served, beforeUserSwitch, usage);
        dirty = true;
      }
    } catch {}
    return result;
  });

  // Price at the end of each turn, off the turn's critical path.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e);
    if ((await ready($)) && dirty) void flush($);
    return result;
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e);
    const text = line();
    if (text === null) return theirs;
    const { Box, Text } = $.ui.resolve(e);
    const row = Text({ dimColor: true, children: [text] });
    return theirs ? Box({ flexDirection: 'column', children: [row, theirs] }) : row;
  });
}
