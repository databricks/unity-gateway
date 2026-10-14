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
    ANTHROPIC_CUSTOM_HEADERS: await $.env.get('ANTHROPIC_CUSTOM_HEADERS'),
  };
}

async function writeEnvironment($: EngineInterface, env: Environment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', env.ENABLE_SMART_ROUTING_V2);
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY);
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', env.ENABLE_SMART_ROUTER_ORCHESTRATOR);
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
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: !enabled ? '0'
      : options.ENABLE_SMART_ROUTING_V2 !== '1' && options.ENABLE_SMART_ROUTING_SUBAGENT_ONLY !== '1'
        ? '1' : original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
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
  on('session.start', async ($, e, next) => {
    const current = await readEnvironment($);
    await setRouting($, options, current.ENABLE_SMART_ROUTING_V2 === '1' ||
      current.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1', false);
    return next(e);
  });

  // The installed smart-router skill already supplies the slash command.
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = e.args.trim();
    if (action !== 'on' && action !== 'off') return {text: 'Usage: /smart-router on|off'};
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
