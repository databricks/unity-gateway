import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { endStatusBandTurn, registerStatusBand, showStatusBand, startStatusBandTurn } from './smart-routing-status.ts';
import { endSubagentRoutingTurn, registerSubagentRouting } from './subagent-routing.ts';
import {
  CLI_AGENT,
  CLI_MODULE,
  DISABLE_ROUTING_FLAG,
  DISABLED_RECIPE,
  ENABLE_ROUTING_FLAG,
  RECIPE_FIELD,
} from './constants.ts';
import { mergeExtraBody } from './extra-body.ts';
import { createIsaacFeedbackState, registerIsaacFeedback } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

type Environment = Record<string, string | undefined>;

// Kept when a host description has no Usage notes section, so continuation stays native.
// Copied from Claude Code 2.1.314's Agent Usage notes; its compact layout (no such section)
// words this differently. Update with tests/integration/test_ug_smart_routing_hooks.py.
// Like the host, add the fork exception only when the host offers fork subagents.
const FORK = 'subagent_type: "fork"';
function continuationNote(host: string): string {
  const fork = host.includes(FORK) ? ` (except ${FORK})` : '';
  return `- To continue a previously spawned agent, use SendMessage with the agent's ID or name as the \`to\` field — that resumes it with full context. A new Agent call starts a fresh agent with no memory of prior runs${fork}, so the prompt must be self-contained.`;
}

async function readEnvironment($: EngineInterface): Promise<Environment> {
  return {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    ENABLE_SMART_ROUTING_SAVINGS: await readSavingsFlag($),
    CLAUDE_CODE_EXTRA_BODY: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
  };
}

async function withSessionControls($: EngineInterface, current: Environment): Promise<Environment> {
  try {
    const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
    if (sessionFile) {
      const overrides = JSON.parse(await $.fs.read(sessionFile));
      for (const key of [
        'ENABLE_SMART_ROUTING_V2',
        'ENABLE_SMART_ROUTING_SUBAGENT_ONLY',
        'ENABLE_SMART_ROUTER_ORCHESTRATOR',
        'ENABLE_SMART_ROUTING_SAVINGS',
      ]) {
        if (typeof overrides?.[key] === 'string') current[key] = overrides[key];
      }
    }
  } catch {
    // Match the routing hooks' inherited-environment fallback for unreadable controls.
  }
  return current;
}

async function readOrchestratorState($: EngineInterface, options: PluginOptions): Promise<boolean> {
  if (!options.orchestratorInstructions) return false;
  const current = await withSessionControls($, await readEnvironment($));
  return isRoutingEnabled(current) && current.ENABLE_SMART_ROUTER_ORCHESTRATOR === '1';
}

async function writeEnvironment($: EngineInterface, env: Environment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', env.ENABLE_SMART_ROUTING_V2);
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', env.ENABLE_SMART_ROUTER_ORCHESTRATOR);
  await $.env.set('ENABLE_SMART_ROUTING_SAVINGS', env.ENABLE_SMART_ROUTING_SAVINGS);
  await $.env.set('CLAUDE_CODE_EXTRA_BODY', env.CLAUDE_CODE_EXTRA_BODY);
}

// The session-control file stays Python's to write: the CLI validates it and replaces it
// atomically. The mod only requests the toggle it just applied to the environment.
async function writeSessionControls(
  $: EngineInterface,
  enabled: boolean,
  sessionControlArgs: readonly string[],
): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const python = await $.env.get('UCODE_SMART_ROUTER_PYTHON');
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (!python || !sessionFile) {
    throw new Error('Smart Router session controls are unavailable for this session.');
  }
  const flag = enabled ? ENABLE_ROUTING_FLAG : DISABLE_ROUTING_FLAG;
  const args = [python, '-m', CLI_MODULE, CLI_AGENT, flag, ...sessionControlArgs];
  const result = await $.process.run(args);
  if (result.exitCode !== 0) {
    throw new Error(`Smart Router controls exited ${result.exitCode}: ${result.stderr.trim()}`);
  }
}

async function setRouting(
  $: EngineInterface,
  options: PluginOptions,
  enabled: boolean,
  syncSessionControls: boolean,
  sessionControlArgs: readonly string[] = [],
): Promise<void> {
  const original = (key: string): string | undefined =>
    (options.unset as readonly string[]).includes(key) ? undefined : options[key] as string;
  const previous = await readEnvironment($);
  const recipe = enabled ? options.recipe : DISABLED_RECIPE;
  if (typeof recipe !== 'string' || /[\r\n]/.test(recipe)) {
    throw new Error('Smart Router recipe must be a single-line string.');
  }
  const environment = {
    ENABLE_SMART_ROUTING_V2: enabled ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    ENABLE_SMART_ROUTING_SAVINGS: enabled ? original('ENABLE_SMART_ROUTING_SAVINGS') : '0',
    CLAUDE_CODE_EXTRA_BODY: mergeExtraBody(previous.CLAUDE_CODE_EXTRA_BODY, {
      [RECIPE_FIELD]: recipe,
    }),
  };
  try {
    await writeEnvironment($, environment);
    // An explicit toggle must also replace the CLI/skill's saved session overrides,
    // which the routing hooks apply on top of the environment and would otherwise win.
    if (syncSessionControls) {
      await writeSessionControls($, enabled, sessionControlArgs);
    }
    $.ui.invalidate('tool.describe');
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}

export function register(on: On, options: PluginOptions): void {
  registerSavings(on, options);
  const feedback = createIsaacFeedbackState();
  const launchedEnabled = isRoutingEnabled(options);
  let orchestratorActive = Boolean(options.orchestratorInstructions);

  on('session.start', async ($, e, next) => {
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, isRoutingEnabled(current), false);
    }
    await savingsSessionStart($);
    return next(e);
  });

  on('prompt.submit', async ($, e, next) => {
    startStatusBandTurn();
    await savingsPromptSubmit($, e);
    return next(e);
  });

  // Claude permits one unmatched turn.step handler, so orchestration and savings share it.
  on('turn.step', async function* ($, e, forward) {
    async function* next(request: typeof e) {
      const agent = str(request.agentId);
      let trackSavings = false;
      let baseline: string | null = null;
      try {
        trackSavings = await ready($);
        if (trackSavings) {
          if (agent === null) {
            lastMainModel = str(request.model) ?? lastMainModel;
            firstMainModel ??= lastMainModel;
            firstPromptSeen = true;
            userSwitched ||= lastMainModel !== firstMainModel;
          } else baseline = lastMainModel ?? (await mainModel($));
        }
      } catch {
        trackSavings = false;
      }
      const beforeUserSwitch = !userSwitched;
      const result = yield* forward(request);
      try {
        const usage = (result as any)?.usage;
        const served = str(request.model) ?? str(usage?.model);
        const base = agent === null ? served : baseline;
        if (trackSavings && await ready($) && usage && served && base) {
          accumulate(agent ?? 'main', base, served, beforeUserSwitch, usage);
          dirty = true;
        }
      } catch {}
      return result;
    }
    const active = await readOrchestratorState($, options);
    if (active !== orchestratorActive) {
      orchestratorActive = active;
      // A CLI/skill toggle changes the session file without notifying this process.
      $.ui.invalidate('tool.describe');
    }
    return yield* next(e);
  });

  on('tool.describe', {tool: ['Agent', 'Task']}, async ($, e, next) => {
    const instructions = options.orchestratorInstructions as string;
    if (!await readOrchestratorState($, options)) return next(e);
    // Preserve the host's waiting and follow-up mechanics without its ownership policy.
    const notes = e.description.split('\n## Usage notes\n')[1]?.split('\n## ')[0].trim() || continuationNote(e.description);
    return next({...e, description: `${instructions}\n\n## Usage notes\n\n${notes}`});
  });

  // The installed smart-router skill already supplies the slash command.
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    const action = e.args.trim();
    if (action !== 'on' && action !== 'off') return {text: 'Usage: /smart-router on|off'};
    if (!launchedEnabled) return {text: 'Smart Router was not enabled for this session.'};
    if (action === 'off') {
      // The mod compiler keeps $ within one file, so bind these before Isaac's guard.
      feedback.readRoutingEnabled = async () => isRoutingEnabled(await readEnvironment($));
      feedback.disableRouting = async (sessionControlArgs) => {
        await setRouting($, options, false, true, sessionControlArgs);
        showStatusBand();
        $.ui.invalidate('ui.render');
      };
    }
    return next(e);
  });
  registerIsaacFeedback(on, feedback, options.isaacExecutable as string, options.isaacFeedbackDirectory as string);
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = e.args.trim();
    try {
      await setRouting($, options, action === 'on', true);
      showStatusBand();
      $.ui.invalidate('ui.render');
    } catch {
      return {text: 'Could not update Smart Router. Check the recipe, CLAUDE_CODE_EXTRA_BODY, and environment permissions; retry or restart through UG.'};
    }
    return {text: `Smart Router is ${action}`};
  });
  registerStatusBand(on, () => savingsShown);
  registerSubagentRouting(on, options);
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    return next(e);
  });
}

// Price turn-step usage by the requested model ID; gateway response IDs can drift.
// Subagents use the current main model as their baseline. First-prompt routing uses
// the user's pre-routing model until a later manual switch. See docs/claude-mod.md.

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
// The pricer's segment: a saving, or a cost increase when routing chose pricier models.
type Estimate = { saved: boolean; amount: string; percent: string };

const num = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);

// Set from the launch's plugin options by register.
let pricer: string[] = [];
let usagePath: string | null = null;
let enabled = false;
let version: string | null = null;

const entries = new Map<string, Entry>();
// The model selected before first-prompt routing. Seeded at launch, then captured
// when the user submits the first prompt so a preceding /model choice is honored.
let startModel: string | null = null;
let firstPromptSeen = false;
// The model the main loop last named, used as the subagent request's baseline.
let lastMainModel: string | null = null;
// The model the main loop's first request named (first-prompt routing's pick).
let firstMainModel: string | null = null;
// Whether a main request has since named another model, which only the user does.
let userSwitched = false;
let seeding: Promise<void> | undefined;
let estimate: Estimate | null = null;
// Whether the current render draws the row, which hides the status band.
let savingsShown = false;
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
    firstPromptSeen ||= saved.first_prompt_seen === true || firstMainModel !== null;
    userSwitched ||= saved.user_switched === true;
  } catch {}
}

async function readSavingsFlag($: EngineInterface): Promise<string | undefined> {
  try {
    return await $.env.get('ENABLE_SMART_ROUTING_SAVINGS');
  } catch {
    return undefined;
  }
}

async function readSavingsState($: EngineInterface): Promise<boolean> {
  if (!enabled) return false;
  const current = await withSessionControls($, {
    ENABLE_SMART_ROUTING_SAVINGS: await readSavingsFlag($),
  });
  return current.ENABLE_SMART_ROUTING_SAVINGS === '1';
}

// State is seeded once per module instance, by whichever hook runs first;
// concurrent hooks wait on the same load.
async function ready($: any): Promise<boolean> {
  try {
    if (!await readSavingsState($)) return false;
    seeding ??= loadUsage($);
    await seeding;
    return true;
  } catch {
    return false;
  }
}

async function savingsPromptSubmit($: EngineInterface, e: any): Promise<void> {
  try {
    if (e.isMeta || e.command || !e.text.trim() || e.text.trimStart().startsWith('/')) return;
    if (!await ready($) || firstPromptSeen) return;
    // This runs before UserPromptSubmit blocks the prompt for the PTY router.
    startModel = await mainModel($);
    firstPromptSeen = true;
    dirty = true;
    void flush($);
  } catch {}
}

function estimateOf(value: any): Estimate | null {
  if (!value || typeof value !== 'object' || typeof value.saved !== 'boolean') return null;
  const amount = str(value.amount);
  const percent = str(value.percent);
  return amount && percent ? { saved: value.saved, amount, percent } : null;
}

// The pricer's last stdout line; a failed run drops the estimate, which would be stale.
async function runPricer($: any): Promise<Estimate | null> {
  try {
    const run = await $.process.run([...pricer, '--usage', usagePath], {
      timeoutMs: PRICER_TIMEOUT_MS,
    });
    if (run?.exitCode === 0) {
      const out = JSON.parse(String(run.stdout ?? '').trim().split('\n').pop() ?? '');
      if (out && typeof out === 'object') return estimateOf(out.savings);
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
        first_prompt_seen: firstPromptSeen,
        user_switched: userSwitched,
        entries: Array.from(entries.values()),
      }),
    );
  } catch (error) {
    // Retry at the next turn.complete rather than pricing a file that is not there.
    dirty = true;
    setEstimate($, null);
    throw error;
  }
  setEstimate($, await runPricer($));
}

function setEstimate($: EngineInterface, next: Estimate | null): void {
  if (JSON.stringify(next) !== JSON.stringify(estimate)) {
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

// Styled like the status band: a bold label, the amount in a pill, a dim italic trailer.
// Nothing until there is an estimate: the band and prompt hint already show routing state.
function savingsRow({ Box, Text }: any): unknown {
  if (!estimate) return null;
  const color = estimate.saved ? 'green' : 'yellow';
  const children: unknown[] = [
    Text({
      bold: true,
      children: [estimate.saved ? '💰 Est. saved with smart routing' : 'Smart routing cost'],
    }),
    Text({ bold: true, color: 'black', backgroundColor: color, children: [` ${estimate.amount} `] }),
    Text({ color, children: [`${estimate.saved ? '' : 'more '}(${estimate.percent})`] }),
  ];
  if (version) {
    children.push(
      Text({ dimColor: true, italic: true, children: [`— Unity Gateway Smart Router v${version}`] }),
    );
  }
  return Box({ flexDirection: 'row', columnGap: 1, children });
}

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
  let savingsActive = false;
  let refresh: { cancel: () => void } | undefined;

  // Price at the end of each turn, off the turn's critical path. The status band holds the
  // event's one unmatched hook, so this one matches every turn instead.
  on('turn.complete', { isAborted: [false, true] }, async ($, e, next) => {
    const result = await next(e);
    if ((await ready($)) && dirty) void flush($);
    return result;
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    savingsActive = await readSavingsState($);
    // CLI toggles change the file without updating Claude's cached render props.
    if (enabled && !refresh) {
      try {
        refresh = $.clock.every(1000, async () => {
          try {
            const active = await readSavingsState($);
            if (active !== savingsActive) {
              savingsActive = active;
              $.ui.invalidate('ui.render');
            }
          } catch {}
        });
      } catch {}
    }
    // Decided before the inner handlers run, so the status band sees it whichever
    // handler Claude Code runs first.
    savingsShown = savingsActive && estimate !== null;
    const theirs = await next(e);
    if (!savingsShown) return theirs;
    const ui = $.ui.resolve(e);
    const row = savingsRow(ui);
    if (row === null) return theirs;
    return theirs ? ui.Box({ flexDirection: 'column', children: [theirs, row] }) : row;
  });
}

async function savingsSessionStart($: EngineInterface): Promise<void> {
  if (await ready($)) {
    startModel ??= await mainModel($);
    void flush($);
  }
}
