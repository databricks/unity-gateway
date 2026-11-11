import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { endStatusBandTurn, registerStatusBand, showStatusBand } from './smart-routing-status.ts';
import { endSubagentRoutingTurn, registerSubagentRouting, routedModels } from './subagent-routing.ts';
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
import { effortProfile, EFFORT_LEVELS } from './auto-effort.ts';
import { isRoutingEnabled } from './routing-state.ts';

type Environment = Record<string, string | undefined>;
type AutoPicker = {
  model: string;
  mainModel: string;
  storeKey: string;
  description: string;
  models: { model: string; label: string; autoModel: string }[];
};

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

type AutoEffortState = {model?: string; drafts: Record<string, string>; native: Record<string, string | undefined>};

async function chooseMainModel($: EngineInterface, auto: AutoPicker, state: AutoEffortState): Promise<void> {
  state.model = await mainModel($, auto);
  state.drafts = {};
  await $.ui.open({ id: 'auto-main', title: 'Choose your main model for Smart Routing', focus: true, closeOnEscape: true, rows: 18 });
  $.ui.invalidate('ui.render');
}

async function syncAutoRouting($: EngineInterface, options: PluginOptions, auto: AutoPicker): Promise<void> {
  const enabled = await autoSelected($, auto);
  const current = isRoutingEnabled(await readSessionEnvironment($));
  if (current !== enabled) await setRouting($, options, enabled, true);
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
  const auto: AutoPicker | undefined = options.autoPicker ? JSON.parse(options.autoPicker as string) : undefined;
  let choosing = false;
  let switchingAuto = false;
  const effortState: AutoEffortState = {drafts:{}, native:{}};
  let pendingSelection: { cancel: () => void } | undefined;
  const feedback = createIsaacFeedbackState();
  const launchedEnabled = isRoutingEnabled(options);
  let orchestratorActive = Boolean(options.orchestratorInstructions);

  on('session.start', async ($, e, next) => {
    if (auto) {
      await $.command.register({ name: 'auto', description: 'Customize main agent model', immediate: true });
      await customAutoRow($, auto, await mainModel($, auto));
      await syncAutoRouting($, options, auto);
    }
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, isRoutingEnabled(current), false);
    }
    return next(e);
  });

  on('turn.step', async function* ($, e, next) {
    if (auto && !e.agentId) await syncAutoRouting($, options, auto);
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
    on('command.run', { command: 'model' }, async ($, e, next) => {
      const result = await next(e);
      if (switchingAuto) return result;
      // Panel commands return an opaque ref, before their transcript is appended.
      // Place the pane while this still carries the user's command context; the
      // confirmation below focuses it after /model closes or dismisses it on Esc.
      // Direct /model <name> commands have no panel handoff. In particular,
      // never reopen a pane after their manual-model confirmation closed it.
      if ((!e.args.trim() && typeof result.ref === 'number') || (e.args.trim() && await autoSelected($, auto))) {
        await chooseMainModel($, auto, effortState);
      }
      return result;
    });
    on('command.run', {command:'effort'}, async ($, e, next) => {
      const result = await next(e);
      if (result.text) effortState.native[await $.session.model()] = result.text;
      $.ui.invalidate('ui.render');
      return result;
    });
    on('ui.focus', {requestId:'auto-main'}, async ($, e, next) => {
      const row = auto.models.find((_, index) => e.element === 'auto-main-model-' + index);
      if (row && effortState.model !== row.model) {
        effortState.model = row.model;
        $.ui.invalidate('ui.render');
      }
      return next(e);
    });
    on('ui.render', { component: 'Pane' }, async ($, e, next) => {
      if (e.requestId !== 'auto-main') return next(e);
      const { Box, Text, Button } = $.ui.resolve(e);
      const layers = await Promise.all([
        $.settings.read({source:'user'}), $.settings.read({source:'project'}),
        $.settings.read({source:'local'}), $.settings.read({source:'flag'}), $.settings.read({source:'policy'}),
      ]);
      const environment = await $.env.get('CLAUDE_CODE_EFFORT_LEVEL');
      const active = await autoSelected($, auto);
      const model = effortState.model ?? await mainModel($, auto);
      const row = auto.models.find(row => row.model === model) ?? auto.models[0];
      const profile = effortProfile(row.autoModel, layers, environment, effortState.native[row.autoModel]);
      const effort = effortState.drafts[row.model] ?? profile.level;
      const editable = active && profile.levels.length > 0 && !profile.locked;
      const adjust = (delta: number) => {
        if (!editable) return;
        const index = Math.max(0, Math.min(profile.levels.length - 1, profile.levels.indexOf(effort) + delta));
        effortState.drafts[row.model] = profile.levels[index];
        $.ui.invalidate('ui.render');
      };
      const save = async (selected: AutoPicker['models'][number]) => {
        const draft = effortState.drafts[selected.model];
        const previous = await mainModel($, auto);
        const active = await autoSelected($, auto);
        switchingAuto = true;
        pendingSelection?.cancel();
        try {
          await customAutoRow($, auto, selected.model);
          if (active) {
            await $.command.run({ command: 'model', args: selected.autoModel });
            if (await $.session.model() !== selected.autoModel) {
              throw new Error('Claude did not accept the selected main model.');
            }
          }
          if (active && typeof draft === 'string' && EFFORT_LEVELS.includes(draft)) {
            const result = await $.command.run({command: 'effort', args: draft});
            if (result.text) effortState.native[selected.autoModel] = result.text;
          }
          await $.store.set(auto.storeKey, selected.model);
        } catch (error) {
          // Preserve the selected profile and the single row after a rejected switch.
          await customAutoRow($, auto, await mainModel($, auto) || previous);
          throw error;
        } finally {
          switchingAuto = false;
        }
        await $.ui.close({ id: 'auto-main' });
        $.ui.invalidate('ui.render');
        $.ui.toast(`Auto (${selected.label}) saved`);
      };
      const symbols: Record<string,string> = {low:'○',medium:'◐',high:'●',xhigh:'◉',max:'✦',auto:'○'};
      const hint = !active ? 'Select Auto in /model to adjust effort'
        : !profile.levels.length ? `Effort not supported for ${row.label}`
        : `${symbols[effort] ?? '○'} ${effort[0].toUpperCase() + effort.slice(1)} effort${!effortState.drafts[row.model] && profile.note ? ` (${profile.note})` : ''}`;
      return Box({ flexDirection: 'column', children: [
        Text({ bold: true, children: ['Choose your main model for Smart Routing'] }),
        Text({ dimColor: true, children: ['Your main conversation uses this model. Smart Routing selects models for subagent tasks.'] }),
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
    on('command.describe', { command: ['smart-router', 'smart-router-orchestrator'] }, ($, e, next) =>
      next({ ...e, isHidden: true }));
    on('skill.prompt', { skill: ['smart-router', 'smart-router-orchestrator'] }, () =>
      ({ text: 'Routing is controlled only by the user selecting Auto in /model. Do not change routing controls through a skill.' }));
    on('command.run', { command: 'auto' }, async ($) => {
      if (choosing) return {};
      choosing = true;
      try {
        await chooseMainModel($, auto, effortState);
      } finally {
        choosing = false;
      }
      return {};
    });
    // The native confirmation also fires when Auto was already selected. A model
    // change hook alone misses that first-use/reselection case. Defer the next
    // native question until the model picker has closed; never self-dispatch /auto.
    on('session.append', { door: 'command' }, async ($, e, next) => {
      const internalSwitch = switchingAuto;
      const result = await next(e);
      const content = e.message?.content;
      const text = Array.isArray(content) ? content.filter((b) => b.type === 'text').map((b) => b.text).join('\n') : '';
      if (/^<local-command-stdout>(?:Set effort level|Current effort level|Effort level)/i.test(text)) {
        effortState.native[await $.session.model()] = text;
        $.ui.invalidate('ui.render');
      }
      const modelEffort = text.match(/^<local-command-stdout>Set model to .*? with `?(low|medium|high|xhigh|max)`? effort/i)?.[1];
      if (modelEffort) effortState.native[await $.session.model()] = 'Effort level: ' + modelEffort;
      if (internalSwitch) return result;
      if (/^<local-command-stdout>Kept model/.test(text)) await $.ui.close({ id: 'auto-main' });
      if (/^<local-command-stdout>Set model to /.test(text)) {
        pendingSelection?.cancel();
        pendingSelection = $.clock.after(0, async () => {
          await syncAutoRouting($, options, auto);
          $.ui.invalidate('ui.render');
          if (!await autoSelected($, auto)) {
            await $.ui.close({ id: 'auto-main' });
            return;
          }
          if (choosing) return;
          choosing = true;
          try {
            await chooseMainModel($, auto, effortState);
          } finally {
            choosing = false;
          }
        });
      }
      return result;
    });
  }

  on('tool.describe', {tool: ['Agent', 'Task']}, async ($, e, next) => {
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
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    if (auto) return { text: 'Choose Auto in /model to enable Smart Routing, or choose a model to turn it off. Use /auto to customize the main agent model.' };
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
  registerStatusBand(on, auto);
  registerSubagentRouting(on, options);
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    return next(e);
  });
}
