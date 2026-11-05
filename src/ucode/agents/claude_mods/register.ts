import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { endStatusBandTurn, registerStatusBand, showStatusBand } from './smart-routing-status.ts';
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
import { createIsaacFeedbackState, registerIsaacFeedback, type FeedbackState } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

type Environment = Record<string, string | undefined>;
export type AutoPolicy = { revision: number; blocked: boolean; picking: boolean };

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

async function readEffectiveEnvironment($: EngineInterface): Promise<Environment> {
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
  const current = await readEffectiveEnvironment($);
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
  autoPicker = false,
): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  const python = await $.env.get('UCODE_SMART_ROUTER_PYTHON');
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (!python || !sessionFile) {
    throw new Error('Smart Router session controls are unavailable for this session.');
  }
  const flag = enabled ? ENABLE_ROUTING_FLAG : DISABLE_ROUTING_FLAG;
  const args = autoPicker
    ? [python, '-m', CLI_MODULE, 'claude-auto-mode', enabled ? 'auto' : 'concrete', ...sessionControlArgs]
    : [python, '-m', CLI_MODULE, CLI_AGENT, flag, ...sessionControlArgs];
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
  const auto = options.EXPOSE_AUTO_IN_MODEL_PICKER === '1';
  // Saving a model can reload launch env; session controls keep the current mode.
  const wasEnabled = isRoutingEnabled(auto ? await readEffectiveEnvironment($) : previous);
  const recipe = enabled ? options.recipe : auto ? 'DISABLED_AUTO' : DISABLED_RECIPE;
  if (typeof recipe !== 'string' || /[\r\n]/.test(recipe)) {
    throw new Error('Smart Router recipe must be a single-line string.');
  }
  const environment = {
    ENABLE_SMART_ROUTING_V2: enabled && !auto ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? auto ? '1' : original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? auto ? '1' : original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    CLAUDE_CODE_EXTRA_BODY: mergeExtraBody(previous.CLAUDE_CODE_EXTRA_BODY, {
      [RECIPE_FIELD]: recipe,
    }),
  };
  try {
    await writeEnvironment($, environment);
    // An explicit toggle must also replace the CLI/skill's saved session overrides,
    // which the routing hooks apply on top of the environment and would otherwise win.
    if (syncSessionControls && (!auto || enabled || wasEnabled)) {
      await writeSessionControls($, enabled, sessionControlArgs, auto);
    }
    $.ui.invalidate('tool.describe');
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}

export function register(on: On, options: PluginOptions): void {
  const auto = options.EXPOSE_AUTO_IN_MODEL_PICKER === '1';
  const policy = {revision: 0, blocked: false, picking: false};
  const feedback = createIsaacFeedbackState();
  feedback.autoPicker = auto;
  const launchedEnabled = isRoutingEnabled(options);
  let orchestratorActive = Boolean(options.orchestratorInstructions);

  on('session.start', async ($, e, next) => {
    if (launchedEnabled || auto) {
      const current = await readEnvironment($);
      await setRouting($, options, isRoutingEnabled(current), false);
    }
    return next(e);
  });

  on('turn.step', async function* ($, e, next) {
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
    if (auto) return {text: 'Use /model to choose Auto or a concrete model.'};
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
  if (auto) {
    registerAutoPicker(on, options, policy, feedback);
    on('command.describe', {command: 'smart-router'}, () => ({isHidden: true}));
  }

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
  registerStatusBand(on, auto, auto ? policy : undefined);
  registerSubagentRouting(on, options, auto ? policy : undefined);
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    return next(e);
  });
}

// Native cancellation and headless selection text on Claude 2.1.317.
// Interactive saved selections use config.set, including same-model picks.
function modelSelected(text: string): boolean {
  const clean = text.replace(/\x1b\[[0-9;]*m/g, '').trim();
  return clean.startsWith('Set model to ');
}

async function applyAutoPolicy($: EngineInterface, enabled: boolean, args: readonly string[] | undefined, options: PluginOptions, policy: AutoPolicy, selected = true) {
  if (!args) return;
  policy.picking = false;
  if (!selected) return;
  // A failed write must never allow inference with a partially applied policy.
  policy.blocked = true;
  policy.revision += 1;
  try {
    await setRouting($, options, enabled, true, args);
    policy.blocked = false;
    showStatusBand();
    $.ui.invalidate('ui.render');
  } catch (error) {
    $.ui.log(`Could not apply the model's Auto policy: ${String(error)}. Dispatch is paused; retry /model or restart through UG.`);
  }
}

async function selectAuto($: EngineInterface, options: PluginOptions, policy: AutoPolicy, runModel?: (model: string) => Promise<{text?: string}>): Promise<{text: string}> {
  policy.picking = false;
  const model = options.autoMainModel;
  if (typeof model !== 'string' || !model.trim() || model.toLowerCase() === 'auto') {
    if (await $.session.model() === 'auto') policy.blocked = true;
    return {text: 'Auto needs a concrete default_model in the managed Claude config. Ask your admin to set it, then restart UG. Use /model M to continue with a concrete model.'};
  }
  policy.blocked = true;
  try {
    const result = runModel ? await runModel(model) : await $.command.run({command: 'model', args: model});
    // Interactive commands return a message reference; the session owns the selected model.
    if ((typeof result.text === 'string' && !modelSelected(result.text)) || await $.session.model() !== model) {
      return {text: 'Could not apply the admin default for Auto. Dispatch is paused; retry /model auto or choose /model M.'};
    }
    await applyAutoPolicy($, true, [], options, policy);
    return {text: policy.blocked ? 'Could not enable Auto. Dispatch is paused; retry /model auto or choose /model M.' : `Auto enabled with admin default main model ${model}.`};
  } catch (error) {
    return {text: `Could not apply Auto: ${String(error)}. Dispatch is paused; retry /model auto or choose /model M.`};
  }
}

function registerAutoPicker(
  on: On, options: PluginOptions, policy: AutoPolicy, feedback: FeedbackState,
): void {
  let pending: readonly string[] | undefined;
  let approved: readonly string[] | undefined;
  let interactive = true;
  let applyAuto = false;
  // Native redraws reuse the original command text, including our intentional save denial.
  const completedOutputs = new Map<string, string | undefined>();

  on('session.start', {isInteractive: false}, ($, e, next) => {
    interactive = e.isInteractive;
    return next(e);
  });

  on('command.run', {command: 'model'}, async ($, e, next) => {
    const args = e.args.trim();
    // Feedback callbacks carry the accepted reason through the native save.
    if (!feedback.submitted) approved = undefined;
    pending = undefined;
    applyAuto = false;
    policy.picking = false;
    if (!args && !interactive) return next(e);
    if (args.toLowerCase() === 'auto') {
      return selectAuto($, options, policy, (model) => next({...e, args: model}));
    }
    // Ask before the native picker: its closing callback cannot place a feedback pane.
    if (!approved && isRoutingEnabled(await readEffectiveEnvironment($))
        && await feedback.requestAutoFeedback!(async (feedbackArgs) => {
      approved = pending = feedbackArgs;
      policy.picking = true;
      const result = await $.command.run({command: 'model', args});
      if (typeof result.text === 'string') {
        approved = undefined;
        await applyAutoPolicy($, false, feedbackArgs, options, policy, modelSelected(result.text));
      }
    })) return {};
    pending = approved ?? [];
    policy.picking = true;
    const result = await next(e);
    // Headless commands return text directly; interactive picker results arrive later.
    if (typeof result.text === 'string') {
      const choice = pending; pending = undefined;
      await applyAutoPolicy($, false, choice, options, policy, modelSelected(result.text));
    }
    return result;
  });

  on('config.set', {key: 'model'}, async ($, e, next) => {
    if (e.value === 'auto') {
      pending = undefined;
      policy.blocked = true;
      applyAuto = true;
      return {deny: 'Applying Auto with the admin default main model.'};
    }
    const result = await next(e);
    const choice = approved ?? pending;
    if (choice && result.deny === undefined) {
      approved = pending = undefined;
      await applyAutoPolicy($, false, choice, options, policy);
    }
    return result;
  });

  on('session.append', {door: 'command'}, async ($, e, next) => {
    if (!policy.picking) return next(e);
    const text = e.message.content.filter((block) => block.type === 'text').map((block) => block.text).join('');
    const output = /^<local-command-stdout>([\s\S]*)<\/local-command-stdout>$/.exec(text);
    if (!output) return next(e);
    const choice = approved ?? pending;
    approved = pending = undefined;
    if (await $.session.model() === 'auto') {
      policy.picking = false;
      policy.blocked = true;
      // The native command must finish appending before another /model command runs.
      $.clock.after(1, async () => {
        const result = await selectAuto($, options, policy);
        $.ui.log(result.text);
        $.ui.invalidate('ui.render');
      });
    } else {
      await applyAutoPolicy($, false, choice, options, policy, modelSelected(output[1]));
    }
    return next(e);
  });

  on('ui.render', {component: 'CommandOutput'}, async ($, e, next) => {
    if (!/^\/?model$/.test(String(e.props.command))) return next(e);
    if (completedOutputs.has(e.requestId)) {
      const text = completedOutputs.get(e.requestId);
      return next(text === undefined ? e : {...e, props: {...e.props, text}});
    }
    completedOutputs.set(e.requestId, undefined);
    // Native session-only picks bypass config.set.
    if (await $.session.model() === 'auto') {
      if (policy.blocked) return next(e);
      pending = undefined;
      const result = await selectAuto($, options, policy);
      completedOutputs.set(e.requestId, result.text);
      return next({...e, props: {...e.props, text: result.text}});
    }
    if (applyAuto) {
      applyAuto = false;
      policy.blocked = true;
      $.clock.after(1, async () => {
        const result = await selectAuto($, options, policy);
        completedOutputs.set(e.requestId, result.text);
        $.ui.invalidate('ui.render');
      });
      const text = 'Applying Auto with the admin default main model.';
      completedOutputs.set(e.requestId, text);
      return next({...e, props: {...e.props, text}});
    }
    const choice = approved ?? pending; pending = undefined;
    if (modelSelected(e.props.text)) approved = undefined;
    await applyAutoPolicy($, false, choice, options, policy, modelSelected(e.props.text));
    return next(e);
  });

}
