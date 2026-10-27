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
import { createIsaacFeedbackState, registerIsaacFeedback } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

type Environment = Record<string, string | undefined>;

async function readEnvironment($: EngineInterface): Promise<Environment> {
  return {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    CLAUDE_CODE_EXTRA_BODY: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
  };
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
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}

export function register(on: On, options: PluginOptions): void {
  const feedback = createIsaacFeedbackState();
  const launchedEnabled = isRoutingEnabled(options);

  on('session.start', async ($, e, next) => {
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, isRoutingEnabled(current), false);
    }
    return next(e);
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
  registerStatusBand(on);
  registerSubagentRouting(on, options);
  // Claude allows one unmatched turn.complete handler per plugin, so both modules share it.
  on('turn.complete', ($, e, next) => {
    endSubagentRoutingTurn(e.isAborted);
    if (endStatusBandTurn()) $.ui.invalidate('ui.render');
    return next(e);
  });
}
