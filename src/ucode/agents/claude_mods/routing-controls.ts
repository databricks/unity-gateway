import type { EngineInterface, PluginOptions } from 'claude-code';
import { CLI_AGENT, CLI_MODULE, DISABLE_ROUTING_FLAG, DISABLED_RECIPE, DISABLED_AUTO_RECIPE, ENABLE_ROUTING_FLAG, RECIPE_FIELD } from './constants.ts';
import { mergeExtraBody } from './extra-body.ts';
import { isRoutingEnabled } from './routing-state.ts';
type Environment = Record<string, string | undefined>;

export async function readEnvironment($: EngineInterface): Promise<Environment> {
  return {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    CLAUDE_CODE_EXTRA_BODY: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
  };
}

export async function readOrchestratorState($: EngineInterface, options: PluginOptions): Promise<boolean> {
  if (!options.orchestratorInstructions) return false;
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

export async function setRouting(
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
  const recipe = enabled ? options.recipe : auto ? DISABLED_AUTO_RECIPE : DISABLED_RECIPE;
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
    if (syncSessionControls) {
      await writeSessionControls($, enabled, sessionControlArgs, auto);
    }
    $.ui.invalidate('tool.describe');
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}

