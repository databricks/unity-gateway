import type { EngineInterface, On, PluginOptions, PromptOrigin } from 'claude-code';
import {
  endStatusBandTurn,
  registerStatusBand,
  settleStartupValidation,
  showStatusBand,
  skipStartupValidation,
  startStartupValidation,
  startStatusBandTurn,
  warnUnsupportedModelList,
  warnValidationFailed,
} from './smart-routing-status.ts';
import { endSubagentRoutingTurn, registerSubagentRouting, routeAgentStream, routedDisplays, routedModels, savedRoutedDisplay } from './subagent-routing.ts';
import {
  CLI_AGENT,
  MODEL_SET_PREFIX,
  MODEL_KEPT_PREFIX,
  CLI_MODULE,
  DISABLE_ROUTING_FLAG,
  ENABLE_ROUTING_FLAG,
  envBool,
  SMART_ROUTER_OFF_REPLY,
  SMART_ROUTER_ON_REPLY,
  OMNI_LAUNCH_MODE,
  UNSUPPORTED_MODEL_LIST_WARNING,
  VALIDATION_FAILED_WARNING,
} from './constants.ts';
import { createIsaacFeedbackState, parseSmartRouterCommand, registerIsaacFeedback } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';
import {
  LOGS_COMMAND_TIMEOUT_MS,
  ROUTING_LOGS_PANE,
  applyRoutingLogsRead,
  logsReaderCommand,
  registerRoutingLogs,
  routingLogsPaneClosed,
  routingLogsPaneOpen,
} from './routing-logs.ts';
import { AUTO_CONFIRMATION, AUTO_MAIN_TITLE, autoTarget, formatAutoConfirmation, isStaticAuto, resolveAutoModel, sameAutoTarget, type AutoPicker } from './auto-model.ts';

// Native transcript contracts; exercised by the real terminal tests on CI's Claude pin.
const MODEL_SWITCH_RETRY = /^The model changed while you were confirming; pick again(?:$|<)/;
const MODEL_SWITCH_CANCELLED = /^(?:Model switch cancelled(?:$|<)|Model switch to .+ was blocked by a PreModelSwitch hook)/;
// Two instruction lines, eight visible options and the overflow count.
const AUTO_MODELS_PANE = {id:'auto-models', title:AUTO_MAIN_TITLE, rows:11, closeOnEscape:true} as const;
const MODEL_COMMAND_ECHO = /^(<command-name>\/model<\/command-name>\s*<command-message>model<\/command-message>\s*<command-args>)([^<]+)(<\/command-args>)$/;

function sameOrigin(a: PromptOrigin, b: PromptOrigin): boolean {
  return a.kind === b.kind
    && (a.kind !== 'plugin' || (b.kind === 'plugin' && a.name === b.name))
    && (a.kind !== 'channel' || (b.kind === 'channel' && a.server === b.server));
}

type Environment = Record<string, string | undefined>;
async function selectedModel($: EngineInterface, auto: AutoPicker): Promise<string> {
  return resolveAutoModel(auto, await $.session.model());
}

async function entryMainModel($: EngineInterface, auto: AutoPicker): Promise<string> {
  const current = await selectedModel($, auto);
  if (isStaticAuto(auto, current) && auto.variant) return auto.variant;
  const row = auto.models.find(row => row.model === current || row.autoModel === current);
  if (row) return row.model;
  const settings = await $.settings.read();
  const usage = await $.session.usage();
  const equivalent = auto.models.find(row => sameAutoTarget(auto, current, row.autoModel, settings.modelOverrides, usage.context?.window));
  return equivalent?.model ?? await mainModel($, auto);
}

async function mainModel($: EngineInterface, auto: AutoPicker): Promise<string> {
  const awaitModel = await selectedModel($, auto);
  if (isStaticAuto(auto, awaitModel) && auto.variant) return auto.variant;
  const active = auto.models.find((row) => row.autoModel === awaitModel);
  if (active) return active.model;
  const saved = await $.store.get(auto.storeKey);
  return auto.models.find((row) => row.model === saved)?.model ?? auto.mainModel;
}

async function autoSelected($: EngineInterface, auto: AutoPicker): Promise<boolean> {
  const selected = await selectedModel($, auto);
  return auto.models.some((row) => row.autoModel === selected);
}

async function customAutoRow($: EngineInterface, auto: AutoPicker, model: string): Promise<void> {
  const row = auto.models.find((row) => row.model === model)!;
  // The static catalog's Auto row is the only Auto identity native selects.
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION', auto.staticModel ?? row.autoModel);
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION_NAME', 'Auto');
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION', auto.description);
}

// Static-catalog mode keeps native on the catalog's single Auto row and holds the
// main model here. A per-model alias selected natively (saved by an earlier
// version, or a resumed session) still counts as Auto with that main model.
async function initStaticVariant($: EngineInterface, auto: AutoPicker): Promise<void> {
  const selected = await selectedModel($, auto);
  const legacy = isStaticAuto(auto, selected) ? undefined : auto.models.find(row => row.autoModel === selected);
  if (legacy) {
    auto.variant = legacy.model;
    return;
  }
  if (auto.variant) return;
  const saved = auto.preferSaved ? await $.store.get(auto.storeKey) : undefined;
  auto.variant = auto.models.find(row => row.model === saved)?.model ?? auto.mainModel;
}

type RoutingSyncState = {model?: string; failedModel?: string};
type ManualOffRequest = {autoModel: string; manualModel: string; phase: 'restoring' | 'feedback' | 'committing'};
type AutoSaveRequest = {
  model: string;
  baseline: string;
  phase: 'running' | 'waiting' | 'saving';
  confirmation?: 'accepted' | 'cancelled';
  finish: () => Promise<void>;
};
async function syncAutoRouting($: EngineInterface, options: PluginOptions, auto: AutoPicker, state: RoutingSyncState): Promise<void> {
  const model = await selectedModel($, auto);
  if (model === state.failedModel) return;
  try {
    const enabled = await autoSelected($, auto);
    const current = isRoutingEnabled(await readSessionEnvironment($));
    // Fresh Auto controls start OFF. Initialize the process too, even when
    // the control file already agrees with a native manual-model selection.
    if (state.model === undefined || current !== enabled) await setRouting($, options, enabled, true);
    if (model !== state.model || current !== enabled) {
      showStatusBand();
      $.ui.invalidate('ui.render');
    }
    state.model = model;
    state.failedModel = undefined;
  } catch (error) {
    state.failedModel = model;
    $.ui.toast(`Could not update Smart Routing: ${String(error)}. Restart ug claude to retry.`);
  }
}

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
    ENABLE_SMART_ROUTER_AUTO_MODE: await $.env.get('ENABLE_SMART_ROUTER_AUTO_MODE'),
    ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: await $.env.get('ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI'),
  };
}

async function readSessionControls($: EngineInterface): Promise<Environment> {
  const current: Environment = {};
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (sessionFile) {
    try {
      const overrides = JSON.parse(await $.fs.read(sessionFile));
      for (const key of [
        'ENABLE_SMART_ROUTING_V2',
        'ENABLE_SMART_ROUTING_SUBAGENT_ONLY',
        'ENABLE_SMART_ROUTER_ORCHESTRATOR',
        'ENABLE_SMART_ROUTING_SAVINGS',
        'ENABLE_SMART_ROUTER_AUTO_MODE',
        'ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI',
      ]) {
        if (typeof overrides?.[key] === 'string') current[key] = overrides[key];
      }
    } catch {
      // Match the routing hooks' inherited-environment fallback for unreadable controls.
    }
  }
  return current;
}

async function readSessionEnvironment($: EngineInterface): Promise<Environment> {
  return { ...await readEnvironment($), ...await readSessionControls($) };
}

// While the Smart Router logs pane is open, a finished turn reruns the reader, so
// decisions routed after the pane opened appear and the relative ages stay fresh. The
// reader runs here rather than in routing-logs because the mod compiler keeps $ only
// within the file that declares the function receiving it.
async function refreshRoutingLogsPane($: EngineInterface, options: PluginOptions): Promise<void> {
  if (!routingLogsPaneOpen()) return;
  try {
    const open = await $.ui.panes();
    if (!Array.isArray(open) || !open.some((pane) => pane?.id === ROUTING_LOGS_PANE)) {
      routingLogsPaneClosed();
      return;
    }
  } catch {
    return;
  }
  let note: string | undefined;
  try {
    const command = logsReaderCommand(options);
    const session = await $.session.id();
    const result = await $.process.run([...command, '--session', session], { timeoutMs: LOGS_COMMAND_TIMEOUT_MS });
    note = applyRoutingLogsRead(result.stdout,
      result.exitCode !== 0 || result.isStdoutTruncated
        ? `logs command exited ${result.exitCode} or truncated output`
        : undefined);
  } catch (error) {
    note = applyRoutingLogsRead('', error instanceof Error ? error.message : 'read failed');
  }
  $.ui.invalidate('ui.render');
  if (note) $.ui.log(note, { to: 'debug' });
}

async function readOrchestratorState($: EngineInterface, options: PluginOptions, snapshot?: Environment): Promise<boolean> {
  if (!options.orchestratorInstructions) return false;
  const current = snapshot ?? await readSessionEnvironment($);
  return isRoutingEnabled(current) && current.ENABLE_SMART_ROUTER_ORCHESTRATOR === '1';
}

async function writeEnvironment($: EngineInterface, env: Environment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', env.ENABLE_SMART_ROUTING_V2);
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', env.ENABLE_SMART_ROUTER_ORCHESTRATOR);
  // Optional accounting must not block routing controls or their rollback.
  try { await $.env.set('ENABLE_SMART_ROUTING_SAVINGS', env.ENABLE_SMART_ROUTING_SAVINGS); } catch {}
  await $.env.set('ENABLE_SMART_ROUTER_AUTO_MODE', env.ENABLE_SMART_ROUTER_AUTO_MODE);
  await $.env.set('ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI', env.ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI);
  // `CLAUDE_CODE_EXTRA_BODY` is never written here: the guard denies that variable to the user
  // tier, and ug's managed recipe mod owns it at the policy tier instead.
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
  const environment = {
    ENABLE_SMART_ROUTING_V2: enabled ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    ENABLE_SMART_ROUTING_SAVINGS: enabled ? original('ENABLE_SMART_ROUTING_SAVINGS') : '0',
    ENABLE_SMART_ROUTER_AUTO_MODE: original('ENABLE_SMART_ROUTER_AUTO_MODE'),
    ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI: enabled ? original('ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI') : '0',
  };
  try {
    await writeEnvironment($, environment);
    // An explicit toggle must also replace the CLI/skill's saved session overrides,
    // which the routing hooks apply on top of the environment and would otherwise win.
    if (syncSessionControls) {
      await writeSessionControls($, enabled, sessionControlArgs);
    }
    savingsControlRevision += 1;
    setSavingsActive($, environment.ENABLE_SMART_ROUTING_SAVINGS === '1');
    $.ui.invalidate('tool.describe');
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}

const VALIDATION_POLL_MS = 100;
// Longer than the Python check's 10 s deadline, which always publishes its result first.
const VALIDATION_WAIT_MS = 11000;
const VALIDATE_COMMAND_TIMEOUT_MS = 16000;

// Persists the unsupported or failed result as a Smart Router logs row; the warning band is transient.
// A resultPath of '-' reads the result from stdin.
async function recordRouteValidation($: EngineInterface, options: PluginOptions, resultPath: string, stdin?: string): Promise<void> {
  const result = await $.process.run(
    [...logsReaderCommand(options), '--session', await $.session.id(), '--record-route-validation', resultPath],
    { timeoutMs: LOGS_COMMAND_TIMEOUT_MS, ...(stdin === undefined ? {} : { stdin }) },
  );
  if (result.exitCode !== 0) $.ui.log(`Smart Router logs: route validation not recorded (exit ${result.exitCode})`, { to: 'debug' });
}

interface ToggleVerdict { disabled: boolean; failed: boolean; record?: string }

// An unreadable verdict fails closed without a logs row.
async function validateToggle($: EngineInterface, command: string[]): Promise<ToggleVerdict> {
  try {
    const result = await $.process.run(command, {
      stdin: JSON.stringify({ session_id: await $.session.id() }),
      timeoutMs: VALIDATE_COMMAND_TIMEOUT_MS,
    });
    const stdout = result.stdout.trim();
    const verdict = result.exitCode === 0 && stdout ? JSON.parse(stdout) : undefined;
    if (typeof verdict?.routing_disabled === 'boolean') {
      return { disabled: verdict.routing_disabled, failed: verdict.validation_failed === true, record: verdict.routing_disabled ? stdout : undefined };
    }
  } catch {}
  return { disabled: true, failed: true };
}

async function watchStartupValidation($: EngineInterface, options: PluginOptions): Promise<void> {
  if (!startStartupValidation()) return;
  try {
    const controls = await $.env.get('UCODE_SESSION_ENV_FILE');
    if (typeof controls !== 'string' || !controls.endsWith('.json')) {
      settleStartupValidation();
      $.ui.invalidate('ui.render');
      return;
    }
    const resultPath = controls.replace(/\.json$/, '.validation.json');
    let complete = false;
    let waited = 0;
    let poll: { cancel: () => void } | undefined;
    const finish = (): void => {
      complete = true;
      settleStartupValidation();
      poll?.cancel();
      $.ui.invalidate('ui.render');
    };
    const check = async (): Promise<void> => {
      if (complete) return;
      let result: any;
      try {
        result = JSON.parse(await $.fs.read(resultPath));
      } catch {}
      if (complete) return;
      if (!result || !('routing_disabled' in result)) {
        if (waited >= VALIDATION_WAIT_MS) finish();
        return;
      }
      complete = true;
      if (result.routing_disabled === true) {
        const failed = result.validation_failed === true;
        if (failed) warnValidationFailed();
        else warnUnsupportedModelList();
        void recordRouteValidation($, options, resultPath).catch(() => {});
        try {
          await setRouting($, options, false, false);
        } catch {}
        if (await $.env.get('ISAAC_LAUNCH_MODE') === OMNI_LAUNCH_MODE) {
          $.ui.log(SMART_ROUTER_OFF_REPLY);
          $.ui.log(failed ? VALIDATION_FAILED_WARNING : UNSUPPORTED_MODEL_LIST_WARNING);
        }
      }
      finish();
    };
    await check();
    if (!complete) {
      poll = $.clock.every(VALIDATION_POLL_MS, () => {
        waited += VALIDATION_POLL_MS;
        return check();
      });
    }
  } catch {
    settleStartupValidation();
  }
}

async function runInternalModel($: EngineInterface, internalModels: string[], model: string) {
  internalModels.push(model);
  try {
    return await $.command.run({command:'model', args:model});
  } catch (error) {
    const index = internalModels.indexOf(model);
    if (index >= 0) internalModels.splice(index, 1);
    throw error;
  }
}

export function register(on: On, options: PluginOptions): void {
  registerSavings(on, options);
  const auto: AutoPicker | undefined = options.ENABLE_SMART_ROUTER_AUTO_MODE === '1' && options.autoPicker ? JSON.parse(options.autoPicker as string) : undefined;
  const routingSync: RoutingSyncState = {};
  let sessionActive = true;
  type MainModelChooser = {
    key: string;
    options?: {value:string; label:string}[];
    choose?: (model: string) => Promise<void>;
  };
  let chooserGeneration = 0;
  let pendingChooser: MainModelChooser | undefined;
  let reopenChooser: (() => Promise<void>) | undefined;
  let awaitingModelChoice = false;
  let switchingAuto = false;
  const internalModels: string[] = [];
  let manualOffRequest: ManualOffRequest | undefined;
  let autoSave: AutoSaveRequest | undefined;
  let publicAutoEcho: { model: string; args: string; origin: PromptOrigin } | undefined;
  // The main model a public /model command enters the static Auto row with.
  let staticEntry: string | undefined;
  let pendingSelection: { cancel: () => void } | undefined;
  const feedback = createIsaacFeedbackState();
  const launchedEnabled = isRoutingEnabled(options);
  if (options.startupValidation !== '1') skipStartupValidation();
  let orchestratorActive = Boolean(options.orchestratorInstructions);

  function clearAutoSave(): void {
    autoSave = undefined;
    switchingAuto = false;
  }

  function resetChooser(): void {
    pendingChooser = undefined;
    awaitingModelChoice = false;
    manualOffRequest = undefined;
    clearAutoSave();
  }

  on('session.start', async ($, e, next) => {
    routedDisplays.records.clear();
    try {
      const session_id = await $.session.id();
      routedDisplays.sessionId = session_id;
      const root = await $.env.get('UCODE_AGENT_LABELS_DIR');
      // Only smart-routed launches save display files.
      routedDisplays.root = root;
      const directory = `${root}/${encodeURIComponent(session_id)}`;
      if (options.ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI !== '0' && root && await $.fs.exists(directory)) for (const entry of await $.fs.list(directory)) {
        if (entry.kind !== 'file' || entry.isLink || !entry.name.startsWith('agent-') || !entry.name.endsWith('.json')) continue;
        try {
          const saved = savedRoutedDisplay(JSON.parse(await $.fs.read(`${directory}/${entry.name}`)), session_id);
          if (!saved || entry.name !== `agent-${encodeURIComponent(saved.agent_id)}.json`) continue;
          routedDisplays.records.set(saved.agent_id, saved);
          await $.fs.write(`${$.plugin.root}/agent-names/${entry.name}`, JSON.stringify(saved));
        } catch {
          $.ui.log('Could not restore a subagent display label; native labels remain where unavailable.');
        }
      }
    } catch {
      $.ui.log('Could not restore subagent display labels; native labels remain where unavailable.');
    }
    $.ui.invalidate('ui.render');
    sessionActive = true;
    savingsSessionActive = true;
    endedSessionId = null;
    resumePending = false;
    try {
      if (auto) {
        await $.command.register({ name: 'auto', description: 'Customize main agent model', immediate: true });
        if (auto.staticModel) await initStaticVariant($, auto);
        await customAutoRow($, auto, await mainModel($, auto));
        await syncAutoRouting($, options, auto, routingSync);
      }
      if (launchedEnabled) {
        const current = await readEnvironment($);
        await setRouting($, options, isRoutingEnabled(current), false);
      }
    } finally {
      // After the launch write, so a disabling result cannot be overwritten by it.
      await watchStartupValidation($, options);
    }
    await savingsSessionStart($);
    return next(e);
  });

  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    sessionActive = false;
    if (sessionUsage) endedUsage.set(sessionUsage.sessionId, sessionUsage);
    // Native keeps the process on clear/resume without another session.start.
    savingsSessionActive = e.reason === 'clear' || e.reason === 'resume';
    resumePending = e.reason === 'resume';
    endedSessionId = str(e.sessionId) ?? sessionUsage?.sessionId ?? null;
    sessionEpoch += 1;
    sessionUsage = null;
    savingsShown = false;
    setEstimate($, null);
    // Clear/resume must not block the next prompt. Process exit waits for accepted
    // usage to reach disk, but never for an optional price calculation.
    const saved = flush($);
    if (!savingsSessionActive) await saved;
    if (pendingChooser || awaitingModelChoice || autoSave) await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => undefined);
    resetChooser();
    internalModels.length = 0;
    pendingSelection?.cancel();
    return next(e);
  });

  on('prompt.submit', async ($, e, next) => {
    startStatusBandTurn();
    await savingsPromptSubmit($, e);
    return next(e);
  });

  // Claude permits one unmatched turn.step handler, so orchestration and savings share it.
  on('turn.step', async function* ($, e, forward) {
    if (auto && !e.agentId && !switchingAuto && !manualOffRequest) await syncAutoRouting($, options, auto, routingSync);
    let current: Environment | undefined;
    if (options.orchestratorInstructions || savingsConfigured) {
      try { current = await readSessionEnvironment($); } catch {}
    }
    async function* trackedStep(request: typeof e) {
      const epoch = sessionEpoch;
      const agent = str(request.agentId);
      let routed = agent !== null && routedModels.model(agent) !== undefined;
      let trackSavings = false;
      let state: SessionUsage | null = null;
      let baseline: string | null = null;
      try {
        trackSavings = await ready($, current);
        state = epoch === sessionEpoch ? sessionUsage : null;
        // Keep selection history while accounting is off. A request may override its
        // model without changing the user's selection for later main/child work.
        if (state) {
          // Restored accounting does not change the model native resumes a child on.
          routed ||= agent !== null && Array.from(state.entries.values()).some(entry => entry.agent === agent && entry.routed === true);
          const selected = await savingsMainModel($);
          if (state !== sessionUsage || !savingsSessionActive) trackSavings = false;
          else {
            if (!selected && trackSavings) markUsageIncomplete($, state);
            if (selected) {
              state.lastMainModel = selected;
              if (agent === null) {
                state.firstMainModel ??= selected;
                state.firstPromptSeen = true;
              }
              state.userSwitched ||= state.firstMainModel !== null && selected !== state.firstMainModel;
              state.dirty = true;
            }
            if (routed) baseline = selected;
          }
        }
      } catch {
        trackSavings = false;
      }
      const beforeUserSwitch = !state?.userSwitched;
      const useStartBaseline = pricer.includes('--baseline-session-start')
        && beforeUserSwitch && (agent === null || routed);
      const startBaseline = state?.startModel ?? null;
      const pendingState = trackSavings ? state : null;
      if (pendingState) {
        pendingState.pendingSteps += 1;
        pendingState.dirty = true;
      }
      let completed = false;
      try {
        const result = yield* forward(request);
        completed = true;
        try {
          const usage = (result as any)?.usage;
          const served = str(request.model) ?? str(usage?.model);
          const base = useStartBaseline ? startBaseline : routed ? baseline : served;
          // A late response still belongs to its captured session; OFF excludes it.
          if (trackSavings && state && await readSavingsState($)) {
            if (!validTokenUsage(usage) || !served || !base) {
              markUsageIncomplete($, state);
            } else {
              accumulate(state, agent ?? 'main', base, served, beforeUserSwitch, routed, usage);
              state.dirty = true;
            }
          }
        } catch {}
        return result;
      } finally {
        // A thrown or cancelled request may have been billed without returning usage.
        if (!completed && trackSavings) {
          try {
            if (state && await readSavingsState($)) markUsageIncomplete($, state);
          } catch {}
        }
        if (pendingState) {
          pendingState.pendingSteps -= 1;
          pendingState.dirty = true;
          if (epoch !== sessionEpoch || pendingState !== sessionUsage || !savingsSessionActive) void flush($);
        }
      }
    }
    if (current !== undefined) {
      const active = await readOrchestratorState($, options, current);
      if (active !== orchestratorActive) {
        orchestratorActive = active;
        // A CLI/skill toggle changes the session file without notifying this process.
        $.ui.invalidate('tool.describe');
      }
    }
    // An Auto alias is a picker identity, not a gateway model. A subagent the router did
    // not route inherits the session's alias, so every loop sends the alias's real target.
    const model = routedModels.model(e.agentId)
      ?? (auto && !e.agentId && await autoSelected($, auto) ? await mainModel($, auto) : undefined)
      ?? (auto ? autoTarget(auto, e.model) : undefined);
    // The stream router takes the few calls it needs, as the validator follows `$` no further.
    const routing = {
      session: {id: () => $.session.id(), model: () => $.session.model()},
      // A server-side auto-mode verdict covers the call as the model wrote it.
      serverReview: async () => await $.env.get('CLAUDE_CODE_AUTO_MODE_SERVER') === '1',
      // Claude's local auto-mode fallback check is off when any of these is set, mirroring its
      // own CT(): any non-empty value for the first two, 1/true/yes/on for DO_NOT_TRACK.
      fallbackOff: async () => Boolean(await $.env.get('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'))
        || Boolean(await $.env.get('DISABLE_TELEMETRY'))
        || ['1', 'true', 'yes', 'on'].includes(`${await $.env.get('DO_NOT_TRACK') ?? ''}`.toLowerCase().trim()),
      routingEnabled: async () => isRoutingEnabled({
        ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
      }),
      process: {run: (command: readonly string[], init: {stdin: string; timeoutMs: number}) => $.process.run(command, init)},
      clock: {now: () => $.clock.now(), every: (ms: number, tick: () => void) => $.clock.every(ms, tick)},
      ui: {invalidate: (name: 'ui.render') => $.ui.invalidate(name), log: (text: string) => $.ui.log(text)},
    };
    return yield* routeAgentStream(routing, e, trackedStep(model ? {...e, model} : e));
  });

  if (auto) {
    on('ui.render', {component:'Pane', requestId:AUTO_MODELS_PANE.id}, ($, e, next) => {
      const chooser = pendingChooser;
      if (!chooser?.options || !chooser.choose) return next(e);
      const {Box, Text, Select} = $.ui.resolve(e);
      const current = chooser.options[0];
      const heading = `${AUTO_MAIN_TITLE} (currently ${current.label}).`;
      // Native Select appends its value and caret after the label. Cover that line with the routing hint.
      return Box({flexDirection:'column', children:[
        Select({
          key:chooser.key, label:'\n', options:chooser.options,
          value:current.value, autoFocus:true, onSelect:chooser.choose,
        }),
        Box({position:'absolute', top:0, left:0, right:0, flexDirection:'column', children:[
          Text({wrap:'truncate-end', children:[heading]}),
          Text({wrap:'truncate-end', children:['Subagents will be smart routed.'.padEnd(e.props.bodyColumns)]}),
        ]}),
      ]});
    });
    on('ui.close', {id:AUTO_MODELS_PANE.id}, ($, e, next) => {
      if (e.origin.kind === 'person') {
        if (autoSave) return {value:undefined};
        pendingChooser = undefined;
      }
      return next(e);
    });
    on('ui.close', {id:'auto-main'}, ($, e, next) => {
      if (feedback.paneId === 'auto-main' && !feedback.submitted) manualOffRequest = undefined;
      return next(e);
    });
    on('session.append', {door:'prompt'}, async ($, e, next) => {
      if (!e.agentId && !e.message.isMeta) {
        if (pendingChooser || awaitingModelChoice || autoSave) await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => undefined);
        resetChooser();
      }
      return next(e);
    });
    on('classic.PreModelSwitch', async ($, e, next) => {
      const result = await next(e);
      if (result.permissionDecision === 'deny' || result.permissionDecision === 'ask' || result.block || result.preventContinuation) return result;
      const to = resolveAutoModel(auto, e.to_model);
      if (resolveAutoModel(auto, e.requested_model) !== to || !auto.models.some(row => row.autoModel === to)) return result;
      const settings = await $.settings.read();
      const usage = await $.session.usage();
      const target = isStaticAuto(auto, to)
        ? auto.models.find(row => row.model === (staticEntry ?? auto.variant))?.autoModel ?? to
        : to;
      return sameAutoTarget(auto, e.from_model, target, settings.modelOverrides, usage.context?.window)
        ? {...result, permissionDecision:'allow'} : result;
    });
    on('command.run', { command: ['model', 'auto'] }, async ($, e, next) => {
      const save = async (selected: AutoPicker['models'][number], chooserRequest: object, baseline: string) => {
        if (autoSave) return;
        manualOffRequest = undefined;
        const active = auto.models.some(row => row.autoModel === baseline);
        const previousMain = isStaticAuto(auto, baseline) && staticEntry ? staticEntry : await mainModel($, auto);
        if (pendingChooser !== chooserRequest || !sessionActive) return;
        pendingChooser = undefined;
        pendingSelection?.cancel();
        const request: AutoSaveRequest = {
          model: selected.autoModel, baseline, phase:'running',
          finish: async () => {
            if (autoSave !== request || request.phase === 'saving') return;
            if (request.confirmation === 'cancelled') {
              await customAutoRow($, auto, await entryMainModel($, auto));
              if (autoSave === request) {
                clearAutoSave();
                await openChooser();
              }
              return;
            }
            request.phase = 'saving';
            try {
              if (await selectedModel($, auto) !== (active ? auto.staticModel ?? selected.autoModel : baseline)) {
                throw new Error('Claude did not accept the selected main model.');
              }
              if (autoSave !== request) return;
              await customAutoRow($, auto, selected.model);
              if (autoSave !== request) return;
              if (auto.staticModel) {
                auto.variant = selected.model;
                // A native confirmation still pending from the /model that opened
                // this chooser must not restore the main model it started from.
                staticEntry = undefined;
              }
              await $.store.set(auto.storeKey, selected.model);
              if (autoSave !== request) return;
              await syncAutoRouting($, options, auto, routingSync);
              if (autoSave !== request) return;
              $.ui.invalidate('ui.render');
              await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => {
                $.ui.toast('Auto saved. Press Esc to close the main model chooser.');
              });
            } catch (error) {
              if (autoSave !== request) return;
              if (auto.staticModel) auto.variant = previousMain;
              if (await selectedModel($, auto) === baseline && autoSave === request) await customAutoRow($, auto, previousMain);
              if (autoSave !== request) return;
              await openChooser();
              $.ui.toast(`Could not save Auto: ${String(error)}. Check /model, then retry.`);
              $.ui.invalidate('ui.render');
            } finally {
              if (autoSave === request) {
                clearAutoSave();
              }
            }
          },
        };
        autoSave = request;
        switchingAuto = true;
        const run = async () => {
          if (autoSave !== request) return;
          try {
            if (active && auto.staticModel && selected.model !== previousMain) {
              const usage = await $.session.usage();
              if (autoSave !== request) return;
              if ((usage.context?.tokens ?? 0) > 0) {
                const answer = await $.ui.ask(
                  `Switch the main model to ${selected.label}? The new model will re-read the full conversation, which may use more tokens.`,
                  {header:'Switch model', options:['Yes, switch model', 'No, go back']},
                ).catch(() => undefined);
                if (autoSave !== request) return;
                if (answer !== 'Yes, switch model') {
                  request.confirmation = 'cancelled';
                  await request.finish();
                  return;
                }
              }
            }
            // The static row stays selected; only the main model it stands for changes.
            if (active && !auto.staticModel && selected.autoModel !== baseline) {
              // Native /model validates only the current custom option without discovery.
              await customAutoRow($, auto, selected.model);
              if (autoSave !== request) return;
              const result = await runInternalModel($, internalModels, selected.autoModel);
              if (autoSave !== request) return;
              // A panel result is not acceptance; native appends its confirmation later.
              const confirmation = result.text?.replace(/^<local-command-stdout>/, '');
              if (confirmation?.startsWith(AUTO_CONFIRMATION)) {
                request.confirmation ??= await selectedModel($, auto) === selected.autoModel ? 'accepted' : 'cancelled';
              } else if (confirmation?.startsWith('Set model to ')) request.confirmation = 'accepted';
              else if (confirmation && (confirmation.startsWith('Kept model as ') || MODEL_SWITCH_CANCELLED.test(confirmation) || MODEL_SWITCH_RETRY.test(confirmation))) request.confirmation = 'cancelled';
              else if (confirmation) throw new Error(confirmation);
              request.phase = 'waiting';
              if (!request.confirmation) return;
            }
            await request.finish();
          } catch (error) {
            if (autoSave !== request) return;
            if (await selectedModel($, auto) === baseline && autoSave === request) await customAutoRow($, auto, previousMain);
            if (autoSave !== request) return;
            clearAutoSave();
            await openChooser();
            $.ui.toast(`Could not save Auto: ${String(error)}. Check /model, then retry.`);
            $.ui.invalidate('ui.render');
          }
        };
        await run();
      };

      async function openChooser(): Promise<void> {
        if (pendingChooser || !sessionActive) return;
        // Native retains a Select's last submitted value by key, including a cancelled switch.
        const request: MainModelChooser = {key:`main-model-${++chooserGeneration}`};
        pendingChooser = request;
        async function recover(error: unknown): Promise<void> {
          if (pendingChooser !== request) return;
          pendingChooser = undefined;
          $.ui.toast(`Could not complete the Auto model selection: ${String(error)}. Try /model auto.`);
          await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => undefined);
        }
        try {
          pendingSelection?.cancel();
          const baseline = await selectedModel($, auto);
          const current = isStaticAuto(auto, baseline) && staticEntry ? staticEntry : await mainModel($, auto);
          const rows = [...auto.models.filter(row => row.model === current), ...auto.models.filter(row => row.model !== current)];
          if (pendingChooser !== request || !sessionActive) return;
          request.options = rows.map(row => ({
            value:row.model, label:rows.some(other => other !== row && other.label === row.label)
              ? `${row.label} (${row.model})` : row.label,
          }));
          const choose = async (model: string) => {
            const selected = rows.find(row => row.model === model);
            if (!selected || pendingChooser !== request) return;
            const current = await selectedModel($, auto);
            if (pendingChooser !== request) return;
            if (current !== baseline) throw new Error('The model changed while the chooser was open');
            // Keep the seat behind native confirmations so cancellation can refocus it at any width.
            await save(selected, request, baseline);
          };
          request.choose = model => choose(model).catch(recover);
          const focusPane = async () => {
            if (pendingChooser !== request) return;
            const pane = await $.ui.open({...AUTO_MODELS_PANE, focus:true});
            if (!pane.isPlaced) throw new Error('Claude could not place the chooser pane');
          };
          await focusPane();
          // Native dismisses /model after this callback and then accepts keyboard focus.
          if (pendingChooser === request) pendingSelection = $.clock.after(0, () => focusPane().catch(recover));
        } catch (error) {
          await recover(error);
        }
      }
      reopenChooser = openChooser;
      if (e.command === 'auto') {
        if (pendingChooser) return {};
        clearAutoSave();
        await openChooser();
        return {};
      }
      const internalCommand = internalModels.includes(e.args)
        && (switchingAuto || (e.origin?.kind === 'plugin' && e.origin.name === 'ug-smart-router'));
      let entry: string | undefined;
      if (!internalCommand) {
        if (pendingChooser || awaitingModelChoice || autoSave) await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => undefined);
        resetChooser();
        awaitingModelChoice = true;
        entry = await entryMainModel($, auto);
        staticEntry = entry;
        await customAutoRow($, auto, entry);
      }
      let command = e;
      publicAutoEcho = undefined;
      if (e.args.trim().toLowerCase() === 'auto') {
        const model = entry ?? await entryMainModel($, auto);
        command = { ...e, args: auto.staticModel ?? auto.models.find((row) => row.model === model)!.autoModel };
        publicAutoEcho = { model: command.args, args: e.args, origin: e.origin };
      }
      let result;
      try {
        result = await next(command);
      } catch (error) {
        publicAutoEcho = undefined;
        throw error;
      }
      if (internalCommand) return result;
      if (!e.args.trim() && typeof result.ref === 'number') {
        // Reserve the seat behind /model. Narrow terminals only seat panes opened by the person's command.
        await $.ui.open(AUTO_MODELS_PANE);
        if (envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))) {
          await $.ui.open({id:'auto-main', title:'Smart Router feedback'});
          await $.ui.close({id:'auto-main'});
        }
      }
      if (e.args.trim().toLowerCase() === 'auto' && awaitingModelChoice && await autoSelected($, auto)) {
        awaitingModelChoice = false;
        await openChooser();
      }
      return result;
    });
    on('command.describe', { command: ['auto', 'smart-router', 'smart-router-orchestrator'] }, ($, e, next) =>
      next({ ...e, isHidden: true }));
    on('skill.prompt', { skill: ['smart-router', 'smart-router-orchestrator'] }, () =>
      ({ text: 'Routing is controlled only by the user selecting Auto in /model. Do not change routing controls through a skill.' }));
    // The native confirmation also fires when Auto was already selected. A model
    // change hook alone misses that first-use/reselection case. Defer the next
    // chooser focus until the model picker has closed; never self-dispatch /auto.
    on('session.append', { door: 'command' }, async ($, e, next) => {
      const content = e.message?.content;
      const text = Array.isArray(content) ? content.filter((b) => b.type === 'text').map((b) => b.text).join('\n') : '';
      const keptModel = text.startsWith(MODEL_KEPT_PREFIX);
      const setModel = text.startsWith(MODEL_SET_PREFIX);
      const stdout = text.replace(/^<local-command-stdout>/, '');
      const retryModel = MODEL_SWITCH_RETRY.test(stdout);
      const cancelledModel = MODEL_SWITCH_CANCELLED.test(stdout);
      const switchAborted = cancelledModel || retryModel;
      const keptOrAborted = keptModel || switchAborted;
      const selected = setModel || keptOrAborted ? await selectedModel($, auto) : undefined;
      const saving = autoSave && !e.agentId
        && ((e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router') || (keptOrAborted && e.origin.kind === 'engine'))
        && ((setModel && selected === autoSave.model) || switchAborted || (keptModel && selected === autoSave.baseline))
        ? autoSave : undefined;
      if (saving) {
        saving.confirmation = keptOrAborted ? 'cancelled' : 'accepted';
        const index = internalModels.indexOf(saving.model);
        if (index >= 0) internalModels.splice(index, 1);
      }
      const internalIndex = selected && e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router'
        ? internalModels.indexOf(selected) : -1;
      const internalSwitch = Boolean(saving) || internalIndex >= 0;
      if (internalIndex >= 0) internalModels.splice(internalIndex, 1);
      if (setModel || keptModel || cancelledModel) {
        // A public selection of the static Auto row enters it with the main model
        // the /model command started from; switches never change the native row.
        if (!internalSwitch && (setModel || keptModel) && selected && isStaticAuto(auto, selected) && staticEntry) auto.variant = staticEntry;
        staticEntry = undefined;
      }
      const previousAuto = routingSync.model;
      const manualModel = setModel && !internalSwitch ? await selectedModel($, auto) : undefined;
      const requested: ManualOffRequest | undefined = manualModel && previousAuto && auto.models.some(row => row.autoModel === previousAuto)
        && !auto.models.some(row => row.autoModel === manualModel)
        && envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))
        && isRoutingEnabled(await readSessionEnvironment($))
        ? {autoModel: previousAuto, manualModel, phase:'restoring'} : undefined;
      const restored = internalSwitch && manualOffRequest?.phase === 'restoring'
        && selected === manualOffRequest.autoModel ? manualOffRequest : undefined;
      const request = requested ?? restored;
      if (!internalSwitch && setModel) manualOffRequest = requested;
      // Close any chooser before forwarding the native confirmation.
      // Forwarding first can dismiss /model and expose the pane for one frame.
      const dismiss = !internalSwitch && (keptModel || cancelledModel || (setModel && !await autoSelected($, auto)));
      if (dismiss) {
        pendingChooser = undefined;
        awaitingModelChoice = false;
        pendingSelection?.cancel();
        await $.ui.close({id:AUTO_MODELS_PANE.id}).catch(() => undefined);
        await $.ui.close({id:'auto-main'}).catch(() => undefined);
        if (setModel && !request) await syncAutoRouting($, options, auto, routingSync);
      }
      const echo = MODEL_COMMAND_ECHO.exec(text);
      const echoModel = echo ? resolveAutoModel(auto, echo[2]) : undefined;
      // Claude 2.1.290 stamps typed native model-command echoes as unclassified.
      const pendingEcho = echo && publicAutoEcho?.model === echoModel
        && (sameOrigin(publicAutoEcho.origin, e.origin)
          || (publicAutoEcho.origin.kind !== 'plugin' && e.origin.kind === 'unclassified'))
        ? publicAutoEcho : undefined;
      const echoArgs = pendingEcho?.args ?? 'Auto';
      const autoEcho = echo && (pendingEcho
        || (e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router')
        || (!e.agentId && e.origin.kind === 'engine' && autoSave?.model === echoModel))
        && auto.models.some((row) => row.autoModel === echoModel);
      if (pendingEcho) publicAutoEcho = undefined;
      const staticConfirmation = Boolean(selected && isStaticAuto(auto, selected));
      const autoConfirmation = !(setModel || keptModel) ? undefined
        : staticConfirmation && auto.variant ? auto.models.find(row => row.model === auto.variant)
        : auto.models.find(row => row.autoModel === selected);
      const confirmation = autoEcho || autoConfirmation ? {
        ...e, message: { ...e.message, content: content.map((block) => block.type === 'text' ? {
          ...block, text: autoEcho ? block.text.replace(MODEL_COMMAND_ECHO, (_match, before, _model, after) => before + echoArgs + after)
            : autoConfirmation ? formatAutoConfirmation(block.text) : block.text,
        } : block) },
      } : e;
      const result = await next(confirmation);
      if (saving) {
        if (saving.phase === 'waiting') $.clock.after(0, saving.finish);
        return result;
      }
      if (request) {
        if (restored) request.phase = 'feedback';
        pendingSelection = $.clock.after(0, async () => {
          if (manualOffRequest !== request) return;
          if (!restored) {
            switchingAuto = true;
            try {
              await runInternalModel($, internalModels, request.autoModel);
              if (await selectedModel($, auto) !== request.autoModel) throw new Error('Claude did not restore Auto.');
            } catch (error) {
              manualOffRequest = undefined;
              $.ui.toast(`Could not restore Auto: ${String(error)}. Check /model before continuing.`);
              return;
            } finally {
              switchingAuto = false;
            }
            return;
          }
          try {
            // Native command.run skips this plugin's own hooks. Bind the existing form directly.
            feedback.readRoutingEnabled = async () => isRoutingEnabled(await readEnvironment($));
            feedback.disableRouting = async (sessionControlArgs) => {
              if (manualOffRequest !== request || await selectedModel($, auto) !== request.autoModel) {
                throw new Error('The model selection changed. Select a model again.');
              }
              request.phase = 'committing';
              switchingAuto = true;
              let disabled = false;
              try {
                await setRouting($, options, false, true, sessionControlArgs);
                disabled = true;
                if (manualOffRequest !== request) throw new Error('The model selection changed while updating routing.');
                await runInternalModel($, internalModels, request.manualModel);
                if (manualOffRequest !== request) throw new Error('The model selection changed while selecting the manual model.');
                if (await selectedModel($, auto) !== request.manualModel) throw new Error('Claude did not accept the manual model.');
                if (manualOffRequest !== request) throw new Error('The model selection changed.');
                routingSync.model = request.manualModel;
                routingSync.failedModel = undefined;
                manualOffRequest = undefined;
              } catch (error) {
                if (manualOffRequest !== request) {
                  if (sessionActive && auto) await syncAutoRouting($, options, auto, routingSync);
                  throw error;
                }
                try {
                  await runInternalModel($, internalModels, request.autoModel);
                  if (await selectedModel($, auto) !== request.autoModel) throw new Error('Claude did not restore Auto.');
                } catch (restoreError) {
                  $.ui.toast(`Could not restore Auto: ${String(restoreError)}. Check /model before continuing.`);
                }
                if (disabled && manualOffRequest === request) {
                  try {
                    await setRouting($, options, true, true);
                  } catch (restoreError) {
                    $.ui.toast(`Could not reenable Smart Routing: ${String(restoreError)}. Restart ug claude to retry.`);
                  }
                }
                $.ui.toast(`Could not select the manual model: ${String(error)}.`);
                throw error;
              } finally {
                switchingAuto = false;
                if (manualOffRequest === request) manualOffRequest = undefined;
              }
              showStatusBand();
              $.ui.invalidate('ui.render');
            };
            feedback.paneId = 'auto-main';
            if (!feedback.openRoutingOff) throw new Error('Feedback controls are unavailable.');
            const error = await feedback.openRoutingOff();
            if (error) {
              feedback.paneId = undefined;
              if (manualOffRequest === request) manualOffRequest = undefined;
              $.ui.toast(error);
            }
          } catch (error) {
            if (manualOffRequest === request) manualOffRequest = undefined;
            feedback.paneId = undefined;
            $.ui.toast(`Could not open the feedback form: ${String(error)}. Retry selecting a model in /model.`);
          }
          $.ui.invalidate('ui.render');
        });
        return result;
      }
      if (dismiss && setModel) $.ui.invalidate('ui.render');
      if (internalSwitch || dismiss) return result;
      if (setModel && awaitingModelChoice) {
        awaitingModelChoice = false;
        await syncAutoRouting($, options, auto, routingSync);
        $.ui.invalidate('ui.render');
        if (await autoSelected($, auto)) await reopenChooser?.();
      }
      return result;
    });
  }

  on('tool.describe', {tool: ['Agent', 'Task']}, async ($, e, next) => {
    if (auto && !await autoSelected($, auto)) return next(e);
    const instructions = options.orchestratorInstructions as string;
    const orchestrating = await readOrchestratorState($, options);
    if (!orchestrating && (!options.routeCommand?.length || !isRoutingEnabled(await readSessionEnvironment($)))) return next(e);
    const guidance = 'Smart Router selects the execution model for each task-role subagent. '
      + "This tool's built-in model choices are not Smart Router's model catalog. "
      + "Do not infer that a requested model is unavailable because it is absent from this tool's choices. "
      + 'For a requested name outside those choices, launch a general-purpose subagent without the model argument. '
      + 'Do not refuse a requested spawn on that basis. After launch, use the launch receipt to identify the model actually used. '
      + 'In ordinary progress updates and answers, identify only the model actually launched. Do not repeat or contrast the requested model, or discuss model availability or tool limitations, unless the user explicitly asks about that difference.';
    if (!orchestrating) return next({...e, description: `${e.description}\n\n${guidance}`});
    // Preserve the host's waiting and follow-up mechanics without its ownership policy.
    const notes = e.description.split('\n## Usage notes\n')[1]?.split('\n## ')[0].trim() || continuationNote(e.description);
    return next({...e, description: `${instructions}\n\n## Usage notes\n\n${notes}\n\n${guidance}`});
  });

  // The installed smart-router skill already supplies the slash command.
  registerRoutingLogs(on, options, launchedEnabled);
  // Omnigent matches the "smart-router" command name exactly; update Omnigent if it changes.
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    const command = parseSmartRouterCommand(e.args);
    if (auto) return { text: 'Choose Auto in /model to enable Smart Routing, or choose a model to turn it off. Use /model auto to customize the main agent model.' };
    if (!command) return {text: 'Usage: /smart-router on|off|logs'};
    if (!launchedEnabled) return {text: 'Smart Router was not enabled for this session.'};
    if (command.action === 'off') {
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
  // External integrations match the "smart-router" command name exactly.
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = parseSmartRouterCommand(e.args)?.action;
    const validateCommand = options.validateCommand as string[] | undefined;
    let enabled = action === 'on';
    try {
      const verdict = enabled && validateCommand?.length ? await validateToggle($, validateCommand) : undefined;
      enabled &&= !verdict?.disabled;
      await setRouting($, options, enabled, true);
      showStatusBand();
      if (verdict?.disabled) {
        if (verdict.failed) warnValidationFailed();
        else warnUnsupportedModelList();
        if (verdict.record) void recordRouteValidation($, options, '-', verdict.record).catch(() => {});
        if (await $.env.get('ISAAC_LAUNCH_MODE') === OMNI_LAUNCH_MODE) $.ui.log(verdict.failed ? VALIDATION_FAILED_WARNING : UNSUPPORTED_MODEL_LIST_WARNING);
      }
      $.ui.invalidate('ui.render');
    } catch {
      return {text: 'Could not update Smart Router. Check the environment permissions; retry or restart through UG.'};
    }
    return {text: enabled ? SMART_ROUTER_ON_REPLY : SMART_ROUTER_OFF_REPLY};
  });
  registerStatusBand(on, auto, () => savingsShown);
  registerSubagentRouting(on, options, auto?.models.map(row => row.autoModel));
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', async ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    const result = await next(e);
    // Start the reader after turn completion, as the spawn hook does, so a slow read cannot
    // hold up completion or status cleanup.
    void refreshRoutingLogsPane($, options).catch(() => {});
    return result;
  });
}

// Price turn-step usage by the requested model ID; gateway response IDs can drift.
// Only routed children use the main model as their baseline; unrouted children
// keep their actual cost in both totals. First-prompt routing preserves the user's
// pre-routing model until a manual switch. See docs/claude-mod.md.

const USAGE_VERSION = 2;
const PRICER_TIMEOUT_MS = 20_000;
const USAGE_SAVE_TIMEOUT_MS = 5_000;
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
  routed?: boolean;
  usage: Totals;
};
// The pricer's segment: a saving, or a cost increase when routing chose pricier models.
type Estimate = { saved: boolean; amount: string; percent: string; expiresAt: number };

const num = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0;

const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null);

// Set from the launch's plugin options by register.
let pricer: string[] = [];
let usageDirectory: string | null = null;
let savingsConfigured = false;
let version: string | null = null;
let savingsSessionActive = true;
let savingsActive = false;
let savingsControlRevision = 0;
let savingsRefresh: { cancel: () => void } | undefined;

function stopSavingsRefresh(): void {
  savingsRefresh?.cancel();
  savingsRefresh = undefined;
}

type SessionUsage = {
  sessionId: string;
  usagePath: string;
  entries: Map<string, Entry>;
  // Captured before first-prompt routing; a preceding /model choice is honored.
  startModel: string | null;
  firstPromptSeen: boolean;
  // Selected main model, used as the routed subagent request's baseline.
  lastMainModel: string | null;
  firstMainModel: string | null;
  userSwitched: boolean;
  // Later totals cannot recover an unmetered response's possibly billed usage.
  incompleteUsage: boolean;
  pendingSteps: number;
  seeding?: Promise<void>;
  // A failed load must never replace an existing snapshot with partial totals.
  canPersist: boolean;
  dirty: boolean;
  writeRevision: number;
};
let sessionUsage: SessionUsage | null = null;
const endedUsage = new Map<string, SessionUsage>();
let sessionEpoch = 0;
let endedSessionId: string | null = null;
let resumePending = false;
let estimate: Estimate | null = null;
// Whether the current render draws the row, which hides the status band.
let savingsShown = false;
let flushing: Promise<void> | undefined;
let rerun = false;
let pricing: Promise<void> | undefined;
let priceRequested: SessionUsage | null = null;

async function savingsMainModel($: any): Promise<string | null> {
  try {
    return str(await $.session.model());
  } catch {
    return null;
  }
}

function accumulate(
  state: SessionUsage,
  agent: string,
  baseline: string,
  served: string,
  beforeUserSwitch: boolean,
  routed: boolean | undefined,
  usage: any,
): void {
  const key = JSON.stringify([agent, baseline, served, beforeUserSwitch, routed]);
  let entry = state.entries.get(key);
  if (!entry) {
    entry = {
      agent,
      baseline,
      served,
      before_user_switch: beforeUserSwitch,
      routed,
      usage: {
        input_tokens: 0,
        output_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
      },
    };
    state.entries.set(key, entry);
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

function validTokenUsage(usage: any): boolean {
  const optionalTokens = (value: unknown): boolean => value === undefined
    || (typeof value === 'number' && Number.isInteger(value) && value >= 0);
  if (!usage || typeof usage !== 'object' || Array.isArray(usage)
    || !TOKEN_KEYS.every(key => optionalTokens(usage[key]))) return false;
  const split = usage.cache_creation;
  return split === undefined || (split !== null && typeof split === 'object'
    && !Array.isArray(split) && CACHE_SPLIT_KEYS.every(key => optionalTokens(split[key])));
}

function validSavedUsage(saved: any): boolean {
  if (!saved || ![1, USAGE_VERSION].includes(saved.version) || !Array.isArray(saved.entries)) return false;
  if (saved.pending_steps !== undefined
    && (!Number.isInteger(saved.pending_steps) || saved.pending_steps < 0)) return false;
  if (saved.session_id !== undefined && str(saved.session_id) === null) return false;
  if (['start_model', 'main_model', 'first_main_model'].some(key =>
    saved[key] != null && str(saved[key]) === null)) return false;
  if (['first_prompt_seen', 'user_switched', 'incomplete_usage'].some(key =>
    saved[key] !== undefined && typeof saved[key] !== 'boolean')) return false;
  return saved.entries.every((item: any) => {
    const usage = item?.usage;
    if (!str(item?.agent) || !str(item?.baseline) || !str(item?.served) || !validTokenUsage(usage)) return false;
    return !['routed', 'before_user_switch'].some(key => item[key] !== undefined && typeof item[key] !== 'boolean');
  });
}

async function loadUsage($: any, state: SessionUsage): Promise<void> {
  try {
    const exists = await $.fs.exists(state.usagePath);
    if (state !== sessionUsage) return;
    if (!exists) {
      state.canPersist = true;
      return;
    }
    const saved = JSON.parse(await $.fs.read(state.usagePath));
    if (state !== sessionUsage || !validSavedUsage(saved)) return;
    if (typeof saved.session_id === 'string' && saved.session_id !== state.sessionId) return;
    state.canPersist = true;
    // Older snapshots cannot establish which session owns their recorded usage.
    if (saved.session_id !== state.sessionId && saved.entries.length) state.incompleteUsage = true;
    for (const item of saved.entries) {
      const routed = typeof item.routed === 'boolean' ? item.routed : undefined;
      const hasTokens = TOKEN_KEYS.some(key => num(item.usage[key]) > 0)
        || CACHE_SPLIT_KEYS.some(key => num(item.usage.cache_creation?.[key]) > 0);
      if (item.agent !== 'main' && routed === undefined && hasTokens) {
        state.incompleteUsage = true;
      }
      // Version 1 did not record whether pre-switch work used the session-start baseline.
      if (saved.version === 1 && hasTokens
        && !(item.agent !== 'main' && routed === false) && item.before_user_switch !== false
        && item.baseline !== str(saved.start_model)) state.incompleteUsage = true;
      accumulate(state, item.agent, item.baseline, item.served, item.before_user_switch === true, routed, item.usage);
      state.dirty = true;
    }
    state.startModel ??= str(saved.start_model);
    state.lastMainModel ??= str(saved.main_model);
    state.firstMainModel ??= str(saved.first_main_model);
    state.firstPromptSeen ||= saved.first_prompt_seen === true || state.firstMainModel !== null;
    state.userSwitched ||= saved.user_switched === true;
    state.incompleteUsage ||= saved.incomplete_usage === true || num(saved.pending_steps) > 0;
    state.dirty ||= saved.version !== USAGE_VERSION || num(saved.pending_steps) > 0;
  } catch {}
}

async function readSavingsFlag($: EngineInterface): Promise<string | undefined> {
  try {
    return await $.env.get('ENABLE_SMART_ROUTING_SAVINGS');
  } catch {
    return undefined;
  }
}

async function readSavingsState($: EngineInterface, snapshot?: Environment): Promise<boolean> {
  if (!savingsConfigured) return false;
  try {
    if (snapshot) return snapshot.ENABLE_SMART_ROUTING_SAVINGS === '1';
    const controls = await readSessionControls($);
    return (controls.ENABLE_SMART_ROUTING_SAVINGS ?? await readSavingsFlag($)) === '1';
  } catch {
    return false;
  }
}

function setSavingsActive($: EngineInterface, active: boolean): void {
  if (savingsActive !== active) {
    savingsActive = active;
    $.ui.invalidate('ui.render');
  }
}

async function refreshSavingsState($: EngineInterface, snapshot?: Environment): Promise<boolean> {
  const state = sessionUsage;
  const revision = savingsControlRevision;
  const active = await readSavingsState($, snapshot);
  if (state !== sessionUsage || !savingsSessionActive) return false;
  // A slow control read cannot undo a native toggle that just completed.
  if (revision === savingsControlRevision) setSavingsActive($, active);
  return savingsActive;
}

// Concurrent hooks share one load; admitted requests keep their originating session.
async function ready($: any, snapshot?: Environment): Promise<boolean> {
  try {
    if (!savingsConfigured || !savingsSessionActive) return false;
    const epoch = sessionEpoch;
    const sessionId = str(await $.session.id());
    if (!sessionId || sessionId === endedSessionId || !savingsSessionActive || epoch !== sessionEpoch) return false;
    if (sessionUsage?.sessionId !== sessionId) {
      endedSessionId = null;
      if (sessionUsage) endedUsage.set(sessionUsage.sessionId, sessionUsage);
      const previous = endedUsage.get(sessionId);
      if (previous) {
        // Reuse the owner while saves or requests are pending. An unfinished load
        // gets a fresh object so its old callback cannot populate the resumed state.
        sessionUsage = previous.canPersist ? previous
          : { ...previous, entries: new Map(), seeding: undefined };
        sessionUsage.dirty = true;
      } else {
        sessionUsage = {
          sessionId, usagePath: `${usageDirectory}/${encodeURIComponent(sessionId)}.json`,
          entries: new Map(), startModel: null, firstPromptSeen: false,
          lastMainModel: null, firstMainModel: null, userSwitched: false,
          incompleteUsage: false, pendingSteps: 0, canPersist: false, dirty: false, writeRevision: 0,
        };
      }
      endedUsage.delete(sessionId);
      setEstimate($, null);
    }
    const state = sessionUsage;
    state.seeding ??= state.canPersist ? Promise.resolve() : loadUsage($, state);
    await state.seeding;
    const active = await refreshSavingsState($, snapshot);
    return state === sessionUsage && savingsSessionActive && state.canPersist && active;
  } catch {
    return false;
  }
}

async function savingsPromptSubmit($: EngineInterface, e: any): Promise<void> {
  try {
    if (e.isMeta || e.command || !e.text.trim() || e.text.trimStart().startsWith('/')) return;
    if (resumePending && savingsSessionActive) {
      endedSessionId = null;
      resumePending = false;
    }
    if (!await ready($)) return;
    const state = sessionUsage;
    if (!state || state.firstPromptSeen) return;
    // This runs before UserPromptSubmit blocks the prompt for the PTY router.
    const selected = await savingsMainModel($);
    if (state !== sessionUsage || !savingsSessionActive) return;
    state.startModel = selected;
    state.firstPromptSeen = true;
    state.dirty = true;
    void flush($);
  } catch {}
}

async function savingsNow($: EngineInterface): Promise<number | undefined> {
  try {
    const current = await $.clock.now();
    return Number.isFinite(current) ? current : undefined;
  } catch {
    return undefined;
  }
}

function estimateOf(output: any, startedAt: number | undefined): Estimate | null {
  const value = output?.savings;
  const validForMs = num(output?.valid_for_ms);
  if (startedAt === undefined || validForMs <= 0) return null;
  if (!value || typeof value !== 'object' || typeof value.saved !== 'boolean') return null;
  const amount = str(value.amount);
  const percent = str(value.percent);
  const expiresAt = startedAt + validForMs;
  return amount && percent && Number.isFinite(expiresAt)
    ? { saved: value.saved, amount, percent, expiresAt } : null;
}

async function expireEstimate($: EngineInterface): Promise<void> {
  const current = estimate;
  if (current === null) return;
  const now = await savingsNow($);
  if (estimate === current && (now === undefined || now >= current.expiresAt)) setEstimate($, null);
}

async function writeUsage($: any, state: SessionUsage): Promise<void> {
  state.dirty = false;
  state.writeRevision += 1;
  try {
    const run = await $.process.run([...pricer, '--usage', state.usagePath, '--write-usage', '--no-price'], {
      timeoutMs: USAGE_SAVE_TIMEOUT_MS,
      stdin: JSON.stringify({
        version: USAGE_VERSION,
        session_id: state.sessionId,
        start_model: state.startModel,
        main_model: state.lastMainModel,
        first_main_model: state.firstMainModel,
        first_prompt_seen: state.firstPromptSeen,
        user_switched: state.userSwitched,
        incomplete_usage: state.incompleteUsage,
        pending_steps: state.pendingSteps,
        entries: Array.from(state.entries.values()),
      }),
    });
    if (run?.exitCode !== 0) throw new Error('Could not persist savings usage.');
    if (state === sessionUsage && savingsSessionActive) {
      if (savingsActive) requestPrice($, state);
      else setEstimate($, null);
    }
  } catch {
    // A later turn or exit can retry without claiming an estimate from stale totals.
    state.dirty = true;
    if (state === sessionUsage && savingsSessionActive) setEstimate($, null);
  }
}

async function priceUsage($: EngineInterface, state: SessionUsage): Promise<void> {
  const revision = state.writeRevision;
  const current = (): boolean => state === sessionUsage && savingsSessionActive
    && state.writeRevision === revision && !state.dirty;
  if (!current() || !savingsActive) return;
  try {
    // Starting before the subprocess makes its remaining lifetime conservative.
    const startedAt = await savingsNow($);
    if (!current()) return;
    const run = await $.process.run([...pricer, '--usage', state.usagePath], { timeoutMs: PRICER_TIMEOUT_MS });
    if (run?.exitCode !== 0) throw new Error('Could not price savings usage.');
    const out = JSON.parse(String(run.stdout ?? '').trim().split('\n').pop() ?? '');
    const next = estimateOf(out, startedAt);
    const now = next === null ? undefined : await savingsNow($);
    if (!current()) return;
    await refreshSavingsState($);
    if (current()) {
      setEstimate($, savingsActive && !state.incompleteUsage && next !== null && now !== undefined && now < next.expiresAt ? next : null);
    }
  } catch {
    if (current()) setEstimate($, null);
  }
}

// One optional pricer at a time; newer saved snapshots replace queued requests.
function requestPrice($: EngineInterface, state: SessionUsage): void {
  priceRequested = state;
  if (pricing) return;
  pricing = (async () => {
    while (priceRequested) {
      const next = priceRequested;
      priceRequested = null;
      await priceUsage($, next);
    }
  })().finally(() => {
    pricing = undefined;
    if (priceRequested) requestPrice($, priceRequested);
  });
}

function markUsageIncomplete($: EngineInterface, state: SessionUsage): void {
  state.incompleteUsage = true;
  state.dirty = true;
  if (state === sessionUsage) setEstimate($, null);
}

function setEstimate($: EngineInterface, next: Estimate | null): void {
  if (next === null) stopSavingsRefresh();
  if (JSON.stringify(next) !== JSON.stringify(estimate)) {
    estimate = next;
    // Claude Code only redraws the band when its props change, not when module state does.
    $.ui.invalidate('ui.render');
  }
}

// Serialize saves, including ending sessions, independently of controls and pricing.
function flush($: EngineInterface): Promise<void> {
  if (!savingsConfigured) return Promise.resolve();
  if (flushing) {
    rerun = true;
    return flushing;
  }
  const pending = (async () => {
    do {
      rerun = false;
      const states = new Map(endedUsage);
      if (sessionUsage) states.set(sessionUsage.sessionId, sessionUsage);
      for (const state of states.values()) {
        if (!state.canPersist) {
          // No request is admitted until loading succeeds; the original file stays untouched.
          if (state.pendingSteps === 0 && endedUsage.get(state.sessionId) === state) endedUsage.delete(state.sessionId);
          continue;
        }
        if (state.dirty) await writeUsage($, state);
        if (!state.dirty && state.pendingSteps === 0 && endedUsage.get(state.sessionId) === state) endedUsage.delete(state.sessionId);
      }
    } while (rerun);
  })();
  flushing = pending.finally(() => {
    flushing = undefined;
    // A hook can ask for another save after the loop drains but before this callback.
    if (rerun) return flush($);
  });
  return flushing;
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
  usageDirectory = str(raw.savings_usage_dir);
  savingsConfigured = pricer.length > 0 && usageDirectory !== null;
  version = str(raw.orchestrator_version);

  // Price at the end of each turn, off the turn's critical path. The status band holds the
  // event's one unmatched hook, so this one matches every turn instead.
  on('turn.complete', { isAborted: [false, true] }, async ($, e, next) => {
    const result = await next(e);
    await ready($);
    if (sessionUsage?.dirty || endedUsage.size) void flush($);
    return result;
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!savingsSessionActive) {
      stopSavingsRefresh();
      savingsShown = false;
      return next(e);
    }
    await expireEstimate($);
    // Only an existing estimate needs refreshes for expiry and file-based off/on toggles.
    if (!savingsSessionActive || estimate === null) {
      stopSavingsRefresh();
    } else if (!savingsRefresh) {
      try {
        savingsRefresh = $.clock.every(1000, async () => {
          await expireEstimate($);
          if (!savingsSessionActive || estimate === null) {
            stopSavingsRefresh();
            return;
          }
          try {
            await refreshSavingsState($);
          } catch {}
        });
      } catch {}
    }
    // Decided before the inner handlers run, so the status band sees it whichever
    // handler Claude Code runs first.
    savingsShown = savingsSessionActive && savingsActive && estimate !== null;
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
    const state = sessionUsage;
    const selected = await savingsMainModel($);
    if (state && state === sessionUsage && savingsSessionActive) {
      if (state.startModel === null && selected !== null) {
        state.startModel = selected;
        state.dirty = true;
      }
      void flush($);
    }
  }
}
