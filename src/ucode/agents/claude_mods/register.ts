import type { EngineInterface, On, PluginOptions } from 'claude-code';
import {
  CLI_AGENT,
  CLI_MODULE,
  DISABLE_ROUTING_FLAG,
  DISABLED_RECIPE,
  ENABLE_ROUTING_FLAG,
  RECIPE_HEADER,
} from './constants.ts';

type Environment = Record<string, string | undefined>;

async function readEnvironment($: EngineInterface): Promise<Environment> {
  return {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    ENABLE_SMART_ROUTING_SAVINGS: await $.env.get('ENABLE_SMART_ROUTING_SAVINGS'),
    ANTHROPIC_CUSTOM_HEADERS: await $.env.get('ANTHROPIC_CUSTOM_HEADERS'),
  };
}

async function writeEnvironment($: EngineInterface, env: Environment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', env.ENABLE_SMART_ROUTING_V2);
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', env.ENABLE_SMART_ROUTER_ORCHESTRATOR);
  await $.env.set('ENABLE_SMART_ROUTING_SAVINGS', env.ENABLE_SMART_ROUTING_SAVINGS);
  await $.env.set('ANTHROPIC_CUSTOM_HEADERS', env.ANTHROPIC_CUSTOM_HEADERS);
}

// The session-control file stays Python's to write: the CLI validates it and replaces it
// atomically. The mod only requests the toggle it just applied to the environment.
async function writeSessionControls($: EngineInterface, enabled: boolean): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const python = await $.env.get('UCODE_SMART_ROUTER_PYTHON');
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (!python || !sessionFile) {
    throw new Error('Smart Router session controls are unavailable for this session.');
  }
  const flag = enabled ? ENABLE_ROUTING_FLAG : DISABLE_ROUTING_FLAG;
  const result = await $.process.run([python, '-m', CLI_MODULE, CLI_AGENT, flag]);
  if (result.exitCode !== 0) {
    throw new Error(`Smart Router controls exited ${result.exitCode}: ${result.stderr.trim()}`);
  }
}

async function setRouting(
  $: EngineInterface,
  options: PluginOptions,
  enabled: boolean,
  syncSessionControls: boolean,
): Promise<void> {
  const original = (key: string): string | undefined =>
    (options.unset as readonly string[]).includes(key) ? undefined : options[key] as string;
  const previous = await readEnvironment($);
  const recipe = enabled ? options.recipe : DISABLED_RECIPE;
  if (typeof recipe !== 'string' || /[\r\n]/.test(recipe)) {
    throw new Error('Smart Router recipe must be a single-line string.');
  }
  const headers = (previous.ANTHROPIC_CUSTOM_HEADERS || '').split('\n').filter(
    line => line.trim() &&
      line.split(':')[0].trim().toLowerCase() !== RECIPE_HEADER.toLowerCase());
  const environment = {
    ENABLE_SMART_ROUTING_V2: enabled ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    ENABLE_SMART_ROUTING_SAVINGS: enabled ? original('ENABLE_SMART_ROUTING_SAVINGS') : '0',
    ANTHROPIC_CUSTOM_HEADERS: [...headers, `${RECIPE_HEADER}: ${recipe}`].join('\n'),
  };
  try {
    await writeEnvironment($, environment);
    // An explicit toggle must also replace the CLI/skill's saved session overrides,
    // which the routing hooks apply on top of the environment and would otherwise win.
    if (syncSessionControls) {
      await writeSessionControls($, enabled);
    }
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}


export function register(on: On, options: PluginOptions): void {
  registerSavings(on, options);
  const launchedEnabled = options.ENABLE_SMART_ROUTING_V2 === '1' ||
    options.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1';

  on('session.start', async ($, e, next) => {
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, current.ENABLE_SMART_ROUTING_V2 === '1' ||
        current.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1', false);
    }
    await savingsSessionStart($);
    return next(e);
  });

  // The installed smart-router skill already supplies the slash command.
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = e.args.trim();
    if (action !== 'on' && action !== 'off') return {text: 'Usage: /smart-router on|off'};
    if (!launchedEnabled) return {text: 'Smart Router was not enabled for this session.'};
    try {
      await setRouting($, options, action === 'on', true);
      const guidance = options.ENABLE_SMART_ROUTER_ORCHESTRATOR !== '1' ? '' : action === 'off'
        ? ' Earlier Smart Router Orchestrator instructions are superseded. Stop automatic delegation; honor explicit subagent requests using native model selection. Existing children can finish.'
        : ' Apply the smart-router-orchestrator skill to further work.';
      return {text: `Smart Router is ${action} for this session.${guidance}`};
    } catch {
      return {text: 'Could not update Smart Router. Check the recipe, ANTHROPIC_CUSTOM_HEADERS, and environment permissions; retry or restart through UG.'};
    }
  });
}

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

// Set from the launch's plugin options by register.
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
  if (!enabled || await $.env.get('ENABLE_SMART_ROUTING_SAVINGS') !== '1') return false;
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
  if (!await ready($)) return;
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

function registerSavings(on: On, options: PluginOptions): void {
  if (options.ENABLE_SMART_ROUTING_SAVINGS !== '1') return;
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
      if (active && await ready($) && usage && served && base) {
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
    if (!enabled || await $.env.get('ENABLE_SMART_ROUTING_SAVINGS') !== '1') return theirs;
    const text = line();
    if (text === null) return theirs;
    const { Box, Text } = $.ui.resolve(e);
    const row = Text({ dimColor: true, children: [text] });
    return theirs ? Box({ flexDirection: 'column', children: [row, theirs] }) : row;
  });
}

async function savingsSessionStart($: EngineInterface): Promise<void> {
  if (await ready($)) {
    startModel ??= await mainModel($);
    void flush($);
  }
}
