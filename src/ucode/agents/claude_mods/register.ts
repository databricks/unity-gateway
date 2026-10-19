import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { parseSessionSwitch, type SessionSwitch } from './session-file.ts';
import { registerStatusBand, showStatusBand } from './smart-routing-status.ts';
import { registerSubagentRouting } from './subagent-routing.ts';
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

async function readSessionSwitch($: EngineInterface): Promise<SessionSwitch | undefined> {
  try {
    // Claude's capability scanner requires literal environment-variable names.
    const file = await $.env.get('UCODE_SESSION_ENV_FILE');
    return file ? parseSessionSwitch(await $.fs.read(file)) : undefined;
  } catch {
    return undefined;
  }
}

// The session file stays Python's to write: the CLI validates it and replaces it atomically.
async function writeSessionSwitch(
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

async function setRecipe($: EngineInterface, options: PluginOptions, enabled: boolean): Promise<void> {
  const recipe = enabled ? options.recipe : DISABLED_RECIPE;
  if (typeof recipe !== 'string' || /[\r\n]/.test(recipe)) {
    throw new Error('Smart Router recipe must be a single-line string.');
  }
  const body = mergeExtraBody(await $.env.get('CLAUDE_CODE_EXTRA_BODY'), { [RECIPE_FIELD]: recipe });
  await $.env.set('CLAUDE_CODE_EXTRA_BODY', body);
}

async function toggle(
  $: EngineInterface,
  options: PluginOptions,
  enabled: boolean,
  sessionControlArgs: readonly string[] = [],
): Promise<void> {
  // Requests carry no recipe until the session has been on, like a session without Smart Router.
  const tagged = enabled || (await readSessionSwitch($))?.everEnabled !== false;
  const previousBody = await $.env.get('CLAUDE_CODE_EXTRA_BODY');
  if (tagged) await setRecipe($, options, enabled);
  try {
    await writeSessionSwitch($, enabled, sessionControlArgs);
  } catch (error) {
    // The switch kept its state, so the recipe must too.
    await $.env.set('CLAUDE_CODE_EXTRA_BODY', previousBody);
    throw error;
  }
}

export function register(on: On, options: PluginOptions): void {
  const feedback = createIsaacFeedbackState();

  on('session.start', async ($, e, next) => {
    const session = await readSessionSwitch($);
    if (session?.everEnabled) await setRecipe($, options, session.enabled);
    return next(e);
  });

  // The installed smart-router skill already supplies the slash command.
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    const action = e.args.trim();
    if (action !== 'on' && action !== 'off') return {text: 'Usage: /smart-router on|off'};
    if (action === 'off') {
      // The mod compiler keeps $ within one file, so bind these before Isaac's guard.
      feedback.readRoutingEnabled = async () => (await readSessionSwitch($))?.enabled !== false;
      feedback.disableRouting = async (sessionControlArgs) => {
        await toggle($, options, false, sessionControlArgs);
        showStatusBand();
        $.ui.invalidate('ui.render');
      };
    }
    return next(e);
  });
  registerIsaacFeedback(on, feedback);
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = e.args.trim();
    try {
      await toggle($, options, action === 'on');
      showStatusBand();
      $.ui.invalidate('ui.render');
    } catch {
      return {text: 'Could not update Smart Router. Check the recipe, CLAUDE_CODE_EXTRA_BODY, and environment permissions; retry or restart through UG.'};
    }
    return {text: `Smart Router is ${action}`};
  });
  registerStatusBand(on);
  registerSubagentRouting(on, options);
}
