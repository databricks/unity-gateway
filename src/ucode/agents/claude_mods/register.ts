import type { EngineInterface, On, PluginOptions, PromptOrigin } from 'claude-code';
import { endStatusBandTurn, registerStatusBand, showStatusBand } from './smart-routing-status.ts';
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
import { registerRoutingLogs } from './routing-logs.ts';
import { effortProfile, EFFORT_LEVELS } from './auto-effort.ts';

// Native transcript contracts; exercised by the real terminal tests on CI's Claude pin.
const EFFORT_STATUS = /^<local-command-stdout>(?:Set effort level|Current effort level|Effort level)/i;
const MODEL_CONFIRMATION = /^(<local-command-stdout>(?:Set model to|Kept model as) )(`+).+?\2([^\n<]*)/;
const MODEL_SWITCH_CANCELLED = /^(?:Model switch cancelled(?:$|<)|Model switch to .+ was blocked by a PreModelSwitch hook|The model changed while you were confirming; pick again(?:$|<))/;
const MODEL_COMMAND_ECHO = /^(<command-name>\/model<\/command-name>\s*<command-message>model<\/command-message>\s*<command-args>)([^<]+)(<\/command-args>)$/;

function sameOrigin(a: PromptOrigin, b: PromptOrigin): boolean {
  return a.kind === b.kind
    && (a.kind !== 'plugin' || (b.kind === 'plugin' && a.name === b.name))
    && (a.kind !== 'channel' || (b.kind === 'channel' && a.server === b.server));
}

type Environment = Record<string, string | undefined>;
export type AutoPicker = {
  model: string;
  mainModel: string;
  storeKey: string;
  description: string;
  models: { model: string; label: string; autoModel: string }[];
};

function sameAutoTarget(auto: AutoPicker, from: string, to: string, overrides: unknown, window?: number): boolean {
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

async function entryMainModel($: EngineInterface, auto: AutoPicker): Promise<string> {
  const current = await $.session.model();
  const row = auto.models.find(row => row.model === current || row.autoModel === current);
  if (row) return row.model;
  const settings = await $.settings.read();
  const usage = await $.session.usage();
  const equivalent = auto.models.find(row => sameAutoTarget(auto, current, row.autoModel, settings.modelOverrides, usage.context?.window));
  return equivalent?.model ?? await mainModel($, auto);
}

async function mainModel($: EngineInterface, auto: AutoPicker): Promise<string> {
  const awaitModel = await $.session.model();
  const active = auto.models.find((row) => row.autoModel === awaitModel);
  if (active) return active.model;
  const saved = await $.store.get(auto.storeKey);
  return auto.models.some((row) => row.model === saved) ? saved as string : auto.mainModel;
}

async function autoSelected($: EngineInterface, auto: AutoPicker): Promise<boolean> {
  const selected = await $.session.model();
  return auto.models.some((row) => row.autoModel === selected);
}

async function customAutoRow($: EngineInterface, auto: AutoPicker, model: string): Promise<void> {
  const row = auto.models.find((row) => row.model === model)!;
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION', row.autoModel);
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION_NAME', 'Auto');
  await $.env.set('ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION', auto.description);
}

type AutoPickerState = {model?: string; drafts: Record<string,string>; native: Record<string,string | undefined>};

async function openMainModel($: EngineInterface): Promise<void> {
  await $.ui.open({ id: 'auto-main', title: 'Choose your main model', focus: true, closeOnEscape: true, rows: 18 });
  $.ui.invalidate('ui.render');
}

async function chooseMainModel($: EngineInterface, auto: AutoPicker, state: AutoPickerState, queryEffort = true): Promise<void> {
  state.model = await mainModel($, auto);
  state.drafts = {};
  // Native commands cannot run inside another command.run hook. Query after
  // that command releases the turn, then refresh the inline controls.
  if (queryEffort) $.clock.after(0, async () => {
    if (!await autoSelected($, auto)) return;
    const model = await $.session.model();
    if (state.native[model]) return;
    const status = await $.command.run({command:'effort', args:'status'});
    if (status.text) state.native[model] = status.text;
    $.ui.invalidate('ui.render');
  });
  await openMainModel($);
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
  const model = await $.session.model();
  if (model === state.failedModel) return;
  try {
    const enabled = await autoSelected($, auto);
    const current = isRoutingEnabled(await readSessionEnvironment($));
    if (current !== enabled) await setRouting($, options, enabled, true);
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

async function readOrchestratorState($: EngineInterface, options: PluginOptions): Promise<boolean> {
  if (!options.orchestratorInstructions) return false;
  const current = await readSessionEnvironment($);
  return isRoutingEnabled(current) && current.ENABLE_SMART_ROUTER_ORCHESTRATOR === '1';
}

async function writeEnvironment($: EngineInterface, env: Environment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', env.ENABLE_SMART_ROUTING_V2);
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', env.ENABLE_SMART_ROUTER_ORCHESTRATOR);
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
  const auto: AutoPicker | undefined = options.ENABLE_SMART_ROUTER_AUTO_MODE === '1' && options.autoPicker ? JSON.parse(options.autoPicker as string) : undefined;
  const routingSync: RoutingSyncState = {};
  let choosing = false;
  let switchingAuto = false;
  const internalModels: string[] = [];
  let sessionActive = true;
  let manualOffRequest: ManualOffRequest | undefined;
  let autoSave: AutoSaveRequest | undefined;
  let publicAutoEcho: { model: string; args: string; origin: PromptOrigin } | undefined;
  const pickerState: AutoPickerState = {drafts:{}, native:{}};
  let pendingSelection: { cancel: () => void } | undefined;
  const feedback = createIsaacFeedbackState();
  const launchedEnabled = isRoutingEnabled(options);
  let orchestratorActive = Boolean(options.orchestratorInstructions);

  on('session.start', async ($, e, next) => {
    sessionActive = true;
    if (auto) {
      await $.command.register({ name: 'auto', description: 'Customize main agent model', immediate: true });
      await customAutoRow($, auto, await mainModel($, auto));
      await syncAutoRouting($, options, auto, routingSync);
    }
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, isRoutingEnabled(current), false);
    }
    return next(e);
  });

  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, ($, e, next) => {
    sessionActive = false;
    manualOffRequest = undefined;
    autoSave = undefined;
    switchingAuto = false;
    internalModels.length = 0;
    pendingSelection?.cancel();
    return next(e);
  });

  on('turn.step', async function* ($, e, next) {
    if (auto && !e.agentId && !switchingAuto && !manualOffRequest) await syncAutoRouting($, options, auto, routingSync);
    const active = await readOrchestratorState($, options);
    if (active !== orchestratorActive) {
      orchestratorActive = active;
      // A CLI/skill toggle changes the session file without notifying this process.
      $.ui.invalidate('tool.describe');
    }
    const model = routedModels.model(e.agentId)
      ?? (auto && !e.agentId && await autoSelected($, auto) ? await mainModel($, auto) : undefined);
    return yield* next(model ? {...e, model} : e);
  });

  if (auto) {
    on('ui.close', {id:'auto-main'}, ($, e, next) => {
      if (feedback.paneId === 'auto-main' && !feedback.submitted) manualOffRequest = undefined;
      if (e.origin.kind === 'person') {
        autoSave = undefined;
        switchingAuto = false;
      }
      return next(e);
    });
    on('session.append', {door:'prompt'}, ($, e, next) => {
      if (!e.agentId && !e.message.isMeta) {
        manualOffRequest = undefined;
        autoSave = undefined;
        switchingAuto = false;
      }
      return next(e);
    });
    on('classic.PreModelSwitch', async ($, e, next) => {
      const result = await next(e);
      if (result.permissionDecision === 'deny' || result.permissionDecision === 'ask' || result.block || result.preventContinuation) return result;
      if (e.requested_model !== e.to_model || !auto.models.some(row => row.autoModel === e.to_model)) return result;
      const settings = await $.settings.read();
      const usage = await $.session.usage();
      return sameAutoTarget(auto, e.from_model, e.to_model, settings.modelOverrides, usage.context?.window)
        ? {...result, permissionDecision:'allow'} : result;
    });
    on('command.run', { command: 'model' }, async ($, e, next) => {
      const internalCommand = internalModels.includes(e.args)
        && (switchingAuto || (e.origin?.kind === 'plugin' && e.origin.name === 'ug-smart-router'));
      let entry: string | undefined;
      if (!internalCommand) {
        manualOffRequest = undefined;
        autoSave = undefined;
        switchingAuto = false;
        entry = await entryMainModel($, auto);
        await customAutoRow($, auto, entry);
      }
      let command = e;
      publicAutoEcho = undefined;
      if (e.args.trim().toLowerCase() === 'auto') {
        const model = entry ?? await entryMainModel($, auto);
        command = { ...e, args: auto.models.find((row) => row.model === model)!.autoModel };
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
      // Panel commands return an opaque ref, before their transcript is appended.
      // Place the pane while this still carries the user's command context; the
      // confirmation below focuses it after /model closes or dismisses it on Esc.
      // Direct /model <name> commands have no panel handoff. In particular,
      // never reopen a pane after their manual-model confirmation closed it.
      if ((!e.args.trim() && typeof result.ref === 'number') || (e.args.trim() && await autoSelected($, auto))) {
        await chooseMainModel($, auto, pickerState, false);
      }
      return result;
    });
    on('command.run', {command:'effort'}, async ($, e, next) => {
      const result = await next(e);
      if (result.text) pickerState.native[await $.session.model()] = result.text;
      $.ui.invalidate('ui.render');
      return result;
    });
    on('ui.focus', {requestId:'auto-main'}, async ($, e, next) => {
      const row = auto.models.find((_, index) => e.element === 'auto-main-model-' + index);
      if (row && pickerState.model !== row.model) {
        pickerState.model = row.model;
        $.ui.invalidate('ui.render');
      }
      return next(e);
    });
    on('ui.scroll', {requestId:'auto-main'}, async ($, e, next) => {
      // Overflow makes the native pane consume arrows as one-row scrolls.
      // Keep those keys on the model list; wheel and page scrolling stay native.
      if (feedback.paneId === 'auto-main') return next(e);
      if (e.origin.kind !== 'person' || e.pointer || Math.abs(e.by) !== 1) return next(e);
      const current = Math.max(0, auto.models.findIndex(row => row.model === pickerState.model));
      const index = Math.max(0, Math.min(auto.models.length - 1, current + e.by));
      const key = 'auto-main-model-' + index;
      const focused = await $.ui.focus({requestId:'auto-main', key});
      if (focused.deny) return next(e);
      await $.ui.scroll({in:'auto-main', to:{key}, block:'nearest'});
      return {};
    });
    on('ui.render', { component: 'Pane' }, async ($, e, next) => {
      if (e.requestId !== 'auto-main' || feedback.paneId === 'auto-main') return next(e);
      const { Box, Text, Button } = $.ui.resolve(e);
      const layers = await Promise.all([
        $.settings.read({source:'user'}), $.settings.read({source:'project'}),
        $.settings.read({source:'local'}), $.settings.read({source:'flag'}), $.settings.read({source:'policy'}),
      ]);
      const environment = await $.env.get('CLAUDE_CODE_EFFORT_LEVEL');
      const active = await autoSelected($, auto);
      const model = pickerState.model ?? await mainModel($, auto);
      const row = auto.models.find(row => row.model === model) ?? auto.models[0];
      const profile = effortProfile(row.autoModel, layers, environment, pickerState.native[row.autoModel]);
      const effort = pickerState.drafts[row.model] ?? profile.level;
      const editable = active && profile.levels.length > 0 && !profile.locked;
      const adjust = (delta: number) => {
        if (!editable) return;
        const index = Math.max(0, Math.min(profile.levels.length - 1, profile.levels.indexOf(effort) + delta));
        pickerState.drafts[row.model] = profile.levels[index];
        $.ui.invalidate('ui.render');
      };
      const save = async (selected: AutoPicker['models'][number]) => {
        if (autoSave) return;
        manualOffRequest = undefined;
        const draft = pickerState.drafts[selected.model];
        const active = await autoSelected($, auto);
        if (autoSave) return;
        const baseline = await $.session.model();
        const previousMain = await mainModel($, auto);
        pendingSelection?.cancel();
        const request: AutoSaveRequest = {
          model: selected.autoModel, baseline, phase:'running',
          finish: async () => {
            if (autoSave !== request || request.phase === 'saving') return;
            if (request.confirmation === 'cancelled') {
              await customAutoRow($, auto, await entryMainModel($, auto));
              if (autoSave === request) {
                autoSave = undefined;
                switchingAuto = false;
                await openMainModel($);
              }
              return;
            }
            request.phase = 'saving';
            try {
              if (await $.session.model() !== (active ? selected.autoModel : baseline)) {
                throw new Error('Claude did not accept the selected main model.');
              }
              if (autoSave !== request) return;
              if (active && typeof draft === 'string' && EFFORT_LEVELS.includes(draft)) {
                const result = await $.command.run({command:'effort', args:draft});
                if (autoSave !== request) return;
                if (result.text && /failed|not supported|invalid|not available/i.test(result.text)) throw new Error(result.text);
                if (result.text) pickerState.native[selected.autoModel] = result.text;
              }
              await customAutoRow($, auto, selected.model);
              if (autoSave !== request) return;
              await $.store.set(auto.storeKey, selected.model);
              if (autoSave !== request) return;
              await syncAutoRouting($, options, auto, routingSync);
              if (autoSave !== request) return;
              await $.ui.close({ id: 'auto-main' });
              $.ui.invalidate('ui.render');
              $.ui.toast(`Auto (${selected.label}) saved`);
            } catch (error) {
              if (autoSave !== request) return;
              if (await $.session.model() === baseline && autoSave === request) await customAutoRow($, auto, previousMain);
              if (autoSave !== request) return;
              await openMainModel($);
              $.ui.toast(`Could not save Auto: ${String(error)}. Check /model and /effort, then retry.`);
              $.ui.invalidate('ui.render');
            } finally {
              if (autoSave === request) {
                autoSave = undefined;
                switchingAuto = false;
              }
            }
          },
        };
        autoSave = request;
        switchingAuto = true;
        const run = async () => {
          if (autoSave !== request) return;
          try {
            if (active) {
              // Native /model validates only the current custom option without discovery.
              await customAutoRow($, auto, selected.model);
              if (autoSave !== request) return;
              const result = await runInternalModel($, internalModels, selected.autoModel);
              if (autoSave !== request) return;
              // A panel result is not acceptance; native appends its confirmation later.
              const confirmation = result.text?.replace(/^<local-command-stdout>/, '');
              if (confirmation && /^(?:Set model to |Set to Auto mode )/.test(confirmation)) request.confirmation = 'accepted';
              else if (confirmation && (/^(?:Kept model as |Kept Auto mode )/.test(confirmation) || MODEL_SWITCH_CANCELLED.test(confirmation))) request.confirmation = 'cancelled';
              else if (confirmation) throw new Error(confirmation);
              request.phase = 'waiting';
              if (!request.confirmation) return;
            }
            await request.finish();
          } catch (error) {
            if (autoSave !== request) return;
            if (await $.session.model() === baseline && autoSave === request) await customAutoRow($, auto, previousMain);
            if (autoSave !== request) return;
            autoSave = undefined;
            switchingAuto = false;
            await openMainModel($);
            $.ui.toast(`Could not save Auto: ${String(error)}. Check /model and /effort, then retry.`);
            $.ui.invalidate('ui.render');
          }
        };
        if (active) {
          await $.ui.close({id:'auto-main'});
          if (autoSave === request) pendingSelection = $.clock.after(0, run);
        } else {
          await run();
        }
      };
      const symbols: Record<string,string> = {low:'○',medium:'◐',high:'●',xhigh:'◉',max:'✦',auto:'○'};
      const hint = !active ? 'Select Auto in /model to adjust effort'
        : !profile.levels.length ? `Effort not supported for ${row.label}`
        : `${symbols[effort] ?? '○'} ${effort[0].toUpperCase() + effort.slice(1)} effort${!pickerState.drafts[row.model] && profile.note ? ` (${profile.note})` : ''}`;
      return Box({ flexDirection: 'column', children: [
        Text({ bold: true, children: ['Choose your main model'] }),
        Text({ dimColor: true, children: ["Subagents are smart routed. Your main agent isn't; choose its model below."] }),
        Box({flexDirection:'column', marginTop:1, children:auto.models.map((item,index) =>
          Button({key:'auto-main-model-' + index,
            plain:true, autoFocus:item.model === model ? true : undefined,
            label:`${item.model === model ? '❯' : ' '} ${item.label}`, onPress:() => save(item)}))}),
        Box({flexDirection:'row', marginY:1, children:[
          Text({children:[hint, ...(editable ? [
            Text({dimColor:true,children:[' · h/l to adjust effort']}),
          ] : [])]}),
          ...(editable ? [
            // Keep pane shortcuts mounted without Claude's automatic button captions.
            Box({width:0,height:0,overflow:'hidden',children:[
              Button({key:'auto-effort-less',plain:true,hotkey:'h',label:'Lower effort',onPress:() => adjust(-1)}),
              Button({key:'auto-effort-more',plain:true,hotkey:'l',label:'Raise effort',onPress:() => adjust(1)}),
            ]}),
          ] : []),
        ]}),
        Text({dimColor:true,children:['Enter to save · Esc to cancel']}),
      ] });
    });
    on('command.describe', { command: ['auto', 'smart-router', 'smart-router-orchestrator'] }, ($, e, next) =>
      next({ ...e, isHidden: true }));
    on('skill.prompt', { skill: ['smart-router', 'smart-router-orchestrator'] }, () =>
      ({ text: 'Routing is controlled only by the user selecting Auto in /model. Do not change routing controls through a skill.' }));
    on('command.run', { command: 'auto' }, async ($) => {
      if (choosing) return {};
      autoSave = undefined;
      switchingAuto = false;
      choosing = true;
      try {
        await chooseMainModel($, auto, pickerState);
      } finally {
        choosing = false;
      }
      return {};
    });
    // The native confirmation also fires when Auto was already selected. A model
    // change hook alone misses that first-use/reselection case. Defer the next
    // native question until the model picker has closed; never self-dispatch /auto.
    on('session.append', { door: 'command' }, async ($, e, next) => {
      const content = e.message?.content;
      const text = Array.isArray(content) ? content.filter((b) => b.type === 'text').map((b) => b.text).join('\n') : '';
      const keptModel = text.startsWith(MODEL_KEPT_PREFIX);
      const setModel = text.startsWith(MODEL_SET_PREFIX);
      const cancelledModel = MODEL_SWITCH_CANCELLED.test(text.replace(/^<local-command-stdout>/, ''));
      const selectedModel = setModel || keptModel || cancelledModel ? await $.session.model() : undefined;
      const saving = autoSave && !e.agentId
        && ((e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router') || ((keptModel || cancelledModel) && e.origin.kind === 'engine'))
        && ((setModel && selectedModel === autoSave.model) || (cancelledModel || (keptModel && selectedModel === autoSave.baseline)))
        ? autoSave : undefined;
      if (saving) {
        saving.confirmation = keptModel || cancelledModel ? 'cancelled' : 'accepted';
        const index = internalModels.indexOf(saving.model);
        if (index >= 0) internalModels.splice(index, 1);
      }
      const internalIndex = selectedModel && e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router'
        ? internalModels.indexOf(selectedModel) : -1;
      const internalSwitch = Boolean(saving) || internalIndex >= 0;
      if (internalIndex >= 0) internalModels.splice(internalIndex, 1);
      const previousAuto = routingSync.model;
      const manualModel = setModel && !internalSwitch ? await $.session.model() : undefined;
      const requested: ManualOffRequest | undefined = manualModel && previousAuto && auto.models.some(row => row.autoModel === previousAuto)
        && !auto.models.some(row => row.autoModel === manualModel)
        && envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))
        && isRoutingEnabled(await readSessionEnvironment($))
        ? {autoModel: previousAuto, manualModel, phase:'restoring'} : undefined;
      const restored = internalSwitch && manualOffRequest?.phase === 'restoring'
        && selectedModel === manualOffRequest.autoModel ? manualOffRequest : undefined;
      const request = requested ?? restored;
      if (!internalSwitch && setModel) manualOffRequest = requested;
      // Close the provisional pane before forwarding the native confirmation.
      // Forwarding first can dismiss /model and expose the pane for one frame.
      const dismiss = !internalSwitch && (keptModel || (setModel && !await autoSelected($, auto)));
      if (dismiss) {
        pendingSelection?.cancel();
        await $.ui.close({ id: 'auto-main' });
        if (setModel && !request) await syncAutoRouting($, options, auto, routingSync);
      }
      const echo = MODEL_COMMAND_ECHO.exec(text);
      // Claude 2.1.290 stamps typed native model-command echoes as unclassified.
      const pendingEcho = echo && publicAutoEcho?.model === echo[2]
        && (sameOrigin(publicAutoEcho.origin, e.origin)
          || (publicAutoEcho.origin.kind !== 'plugin' && e.origin.kind === 'unclassified'))
        ? publicAutoEcho : undefined;
      const echoArgs = pendingEcho?.args ?? 'Auto';
      const autoEcho = echo && (pendingEcho
        || (e.origin.kind === 'plugin' && e.origin.name === 'ug-smart-router')
        || (!e.agentId && e.origin.kind === 'engine' && autoSave?.model === echo[2]))
        && auto.models.some((row) => row.autoModel === echo[2]);
      if (pendingEcho) publicAutoEcho = undefined;
      const autoConfirmation = setModel || keptModel ? auto.models.find(row => row.autoModel === selectedModel) : undefined;
      const confirmation = autoEcho || autoConfirmation ? {
        ...e, message: { ...e.message, content: content.map((block) => block.type === 'text' ? {
          ...block, text: autoEcho ? block.text.replace(MODEL_COMMAND_ECHO, (_match, before, _model, after) => before + echoArgs + after)
            : autoConfirmation ? block.text.replace(MODEL_CONFIRMATION, (_match, _before, _quotes, details) => {
              const model = autoConfirmation.model.replace(/^system\.ai\./, '');
              const suffix = details.trim().replace(/^and /, '');
              return `<local-command-stdout>${setModel ? 'Set to' : 'Kept'} Auto mode with main model \`${model}\`. Subagents are smart routed.`
                + (suffix ? `\n${suffix[0].toUpperCase()}${suffix.slice(1)}` : '');
            }) : block.text,
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
              if (await $.session.model() !== request.autoModel) throw new Error('Claude did not restore Auto.');
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
              if (manualOffRequest !== request || await $.session.model() !== request.autoModel) {
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
                if (await $.session.model() !== request.manualModel) throw new Error('Claude did not accept the manual model.');
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
                  if (await $.session.model() !== request.autoModel) throw new Error('Claude did not restore Auto.');
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
            // Native panes first opened by a person remain placeable at 110 columns.
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
      if (EFFORT_STATUS.test(text)) {
        pickerState.native[await $.session.model()] = text;
        $.clock.after(0, async () => {
          if (!(await $.ui.panes()).some(pane => pane.id === 'auto-main')) return;
          await $.ui.open({id:'auto-main', title:'Choose your main model', focus:true, closeOnEscape:true, rows:18});
          const index = Math.max(0, auto.models.findIndex(row => row.model === pickerState.model));
          await $.ui.focus({requestId:'auto-main', key:'auto-main-model-' + index});
        });
        $.ui.invalidate('ui.render');
      }
      if (dismiss && setModel) $.ui.invalidate('ui.render');
      if (internalSwitch || dismiss) return result;
      if (setModel) {
        pendingSelection?.cancel();
        pendingSelection = $.clock.after(0, async () => {
          await syncAutoRouting($, options, auto, routingSync);
          $.ui.invalidate('ui.render');
          if (!await autoSelected($, auto)) {
            await $.ui.close({ id: 'auto-main' });
            return;
          }
          if (choosing) return;
          choosing = true;
          try {
            await chooseMainModel($, auto, pickerState);
          } finally {
            choosing = false;
          }
        });
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
  registerStatusBand(on, auto);
  registerSubagentRouting(on, options, auto?.models.map(row => row.autoModel));
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    return next(e);
  });
}
