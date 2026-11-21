import type { EngineInterface, On, PluginOptions, PromptOrigin } from 'claude-code';
import { endStatusBandTurn, registerStatusBand, showStatusBand, startStatusBandTurn } from './smart-routing-status.ts';
import { endSubagentRoutingTurn, registerSubagentRouting, routedModels } from './subagent-routing.ts';
import {
  CLI_AGENT,
  MODEL_SET_PREFIX,
  MODEL_KEPT_PREFIX,
  CLI_MODULE,
  DISABLE_ROUTING_FLAG,
  DISABLED_RECIPE,
  ENABLE_ROUTING_FLAG,
  envBool,
  RECIPE_FIELD,
  SMART_ROUTER_OFF_REPLY,
  SMART_ROUTER_ON_REPLY,
} from './constants.ts';
import { mergeExtraBody } from './extra-body.ts';
import { createIsaacFeedbackState, parseSmartRouterCommand, registerIsaacFeedback } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';
import {
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
    CLAUDE_CODE_EXTRA_BODY: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
  };
}

async function readSessionEnvironment($: EngineInterface): Promise<Environment> {
  const current = await readEnvironment($);
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
      ]) {
        if (typeof overrides?.[key] === 'string') current[key] = overrides[key];
      }
    } catch {
      // Match the routing hooks' inherited-environment fallback for unreadable controls.
    }
  }
  return current;
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
    const result = await $.process.run([...command, '--session', session], { timeoutMs: 10000 });
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
  await $.env.set('ENABLE_SMART_ROUTING_SAVINGS', env.ENABLE_SMART_ROUTING_SAVINGS);
  await $.env.set('ENABLE_SMART_ROUTER_AUTO_MODE', env.ENABLE_SMART_ROUTER_AUTO_MODE);
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
    ENABLE_SMART_ROUTER_AUTO_MODE: original('ENABLE_SMART_ROUTER_AUTO_MODE'),
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
    sessionActive = true;
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
    await savingsSessionStart($);
    return next(e);
  });

  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    sessionActive = false;
    stopSavingsRefresh();
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
    let current: Environment = {};
    if (options.orchestratorInstructions || enabled) {
      try { current = await readSessionEnvironment($); } catch {}
    }
    async function* next(request: typeof e) {
      const agent = str(request.agentId);
      const routed = agent !== null && routedModels.model(agent) !== undefined;
      let trackSavings = false;
      let baseline: string | null = null;
      try {
        trackSavings = await ready($, current);
        // Keep selection history while accounting is off. A request may override its
        // model without changing the user's selection for later main/child work.
        if (enabled) {
          const selected = await savingsMainModel($);
          if (selected) {
            lastMainModel = selected;
            if (agent === null) {
              firstMainModel ??= selected;
              firstPromptSeen = true;
            }
            userSwitched ||= firstMainModel !== null && selected !== firstMainModel;
            dirty = true;
          }
          if (routed) baseline = selected ?? lastMainModel;
        }
      } catch {
        trackSavings = false;
      }
      const beforeUserSwitch = !userSwitched;
      let completed = false;
      try {
        const result = yield* forward(request);
        completed = true;
        try {
          const usage = (result as any)?.usage;
          const served = str(request.model) ?? str(usage?.model);
          const base = routed ? baseline : served;
          // Session controls can change while the model request is in flight.
          if (trackSavings && await readSavingsState($)) {
            if (!usage || !served || !base) {
              markUsageIncomplete($);
            } else {
              accumulate(agent ?? 'main', base, served, beforeUserSwitch, routed, usage);
              dirty = true;
            }
          }
        } catch {}
        return result;
      } finally {
        // A thrown or cancelled request may have been billed without returning usage.
        if (!completed && trackSavings) {
          try {
            if (await readSavingsState($)) markUsageIncomplete($);
          } catch {}
        }
      }
    }
    const active = await readOrchestratorState($, options, current);
    if (active !== orchestratorActive) {
      orchestratorActive = active;
      // A CLI/skill toggle changes the session file without notifying this process.
      $.ui.invalidate('tool.describe');
    }
    // An Auto alias is a picker identity, not a gateway model. A subagent the router did
    // not route inherits the session's alias, so every loop sends the alias's real target.
    const model = routedModels.model(e.agentId)
      ?? (auto && !e.agentId && await autoSelected($, auto) ? await mainModel($, auto) : undefined)
      ?? (auto ? autoTarget(auto, e.model) : undefined);
    return yield* next(model ? {...e, model} : e);
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
  // Omnigent matches the "smart-router" command name exactly; update Omnigent if it changes.
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = parseSmartRouterCommand(e.args)?.action;
    try {
      await setRouting($, options, action === 'on', true);
      showStatusBand();
      $.ui.invalidate('ui.render');
    } catch {
      return {text: 'Could not update Smart Router. Check the recipe, CLAUDE_CODE_EXTRA_BODY, and environment permissions; retry or restart through UG.'};
    }
    return {text: action === 'on' ? SMART_ROUTER_ON_REPLY : SMART_ROUTER_OFF_REPLY};
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
  routed?: boolean;
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
let sessionActive = true;
let savingsRefresh: { cancel: () => void } | undefined;

function stopSavingsRefresh(): void {
  savingsRefresh?.cancel();
  savingsRefresh = undefined;
}

const entries = new Map<string, Entry>();
// The model selected before first-prompt routing. Seeded at launch, then captured
// when the user submits the first prompt so a preceding /model choice is honored.
let startModel: string | null = null;
let firstPromptSeen = false;
// The selected main model, used as the subagent request's baseline.
let lastMainModel: string | null = null;
// The selected model at the first main request (first-prompt routing's pick).
let firstMainModel: string | null = null;
// Whether the selected main model has changed since the first request.
let userSwitched = false;
// An unmetered response may still be billed; later totals cannot recover its usage.
let incompleteUsage = false;
let seeding: Promise<void> | undefined;
let estimate: Estimate | null = null;
// Whether the current render draws the row, which hides the status band.
let savingsShown = false;
let dirty = false;
let running = false;
let rerun = false;

async function savingsMainModel($: any): Promise<string | null> {
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
  routed: boolean | undefined,
  usage: any,
): void {
  const key = JSON.stringify([agent, baseline, served, beforeUserSwitch, routed]);
  let entry = entries.get(key);
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
        const routed = typeof item.routed === 'boolean' ? item.routed : undefined;
        if (agent !== 'main' && routed === undefined && TOKEN_KEYS.some(key => num(item.usage[key]) > 0)) {
          incompleteUsage = true;
        }
        accumulate(agent, baseline, served, item.before_user_switch === true, routed, item.usage);
        dirty = true;
      }
    }
    startModel ??= str(saved.start_model);
    lastMainModel ??= str(saved.main_model);
    firstMainModel ??= str(saved.first_main_model);
    firstPromptSeen ||= saved.first_prompt_seen === true || firstMainModel !== null;
    userSwitched ||= saved.user_switched === true;
    incompleteUsage ||= saved.incomplete_usage === true;
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
  if (!enabled) return false;
  try {
    const current = snapshot ?? await readSessionEnvironment($);
    return current.ENABLE_SMART_ROUTING_SAVINGS === '1';
  } catch {
    return false;
  }
}

// State is seeded once per module instance, by whichever hook runs first;
// concurrent hooks wait on the same load.
async function ready($: any, snapshot?: Environment): Promise<boolean> {
  try {
    if (!enabled) return false;
    seeding ??= loadUsage($);
    await seeding;
    return await readSavingsState($, snapshot);
  } catch {
    return false;
  }
}

async function savingsPromptSubmit($: EngineInterface, e: any): Promise<void> {
  try {
    if (e.isMeta || e.command || !e.text.trim() || e.text.trimStart().startsWith('/')) return;
    if (!await ready($) || firstPromptSeen) return;
    // This runs before UserPromptSubmit blocks the prompt for the PTY router.
    startModel = await savingsMainModel($);
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

async function writeAndPrice($: any): Promise<void> {
  dirty = false;
  try {
    const run = await $.process.run([...pricer, '--usage', usagePath, '--write-usage'], {
      timeoutMs: PRICER_TIMEOUT_MS,
      stdin: JSON.stringify({
        version: USAGE_VERSION,
        start_model: startModel,
        main_model: lastMainModel,
        first_main_model: firstMainModel,
        first_prompt_seen: firstPromptSeen,
        user_switched: userSwitched,
        incomplete_usage: incompleteUsage,
        entries: Array.from(entries.values()),
      }),
    });
    if (run?.exitCode !== 0) throw new Error('Could not persist savings usage.');
    const out = JSON.parse(String(run.stdout ?? '').trim().split('\n').pop() ?? '');
    setEstimate($, incompleteUsage ? null : estimateOf(out?.savings));
  } catch (error) {
    // Retry at the next turn.complete rather than pricing a file that is not there.
    dirty = true;
    setEstimate($, null);
    throw error;
  }
}

function markUsageIncomplete($: EngineInterface): void {
  incompleteUsage = true;
  dirty = true;
  setEstimate($, null);
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

  // Price at the end of each turn, off the turn's critical path. The status band holds the
  // event's one unmatched hook, so this one matches every turn instead.
  on('turn.complete', { isAborted: [false, true] }, async ($, e, next) => {
    const result = await next(e);
    if ((await ready($)) && dirty) void flush($);
    return result;
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!sessionActive) {
      stopSavingsRefresh();
      savingsShown = false;
      return next(e);
    }
    savingsActive = await readSavingsState($);
    // Only an existing estimate needs redraws for file-based off/on toggles.
    if (!sessionActive || estimate === null) {
      stopSavingsRefresh();
    } else if (!savingsRefresh) {
      try {
        savingsRefresh = $.clock.every(1000, async () => {
          if (!sessionActive || estimate === null) {
            stopSavingsRefresh();
            return;
          }
          try {
            const active = await readSavingsState($);
            if (sessionActive && estimate !== null && active !== savingsActive) {
              savingsActive = active;
              $.ui.invalidate('ui.render');
            }
          } catch {}
        });
      } catch {}
    }
    // Decided before the inner handlers run, so the status band sees it whichever
    // handler Claude Code runs first.
    savingsShown = sessionActive && savingsActive && estimate !== null;
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
    startModel ??= await savingsMainModel($);
    void flush($);
  }
}
