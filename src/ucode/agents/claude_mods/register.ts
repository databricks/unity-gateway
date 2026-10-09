import type { EngineInterface, On, PluginOptions } from 'claude-code';

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

async function setRouting($: EngineInterface, options: PluginOptions, enabled: boolean): Promise<void> {
  const original = (key: string): string | undefined =>
    (options.unset as readonly string[]).includes(key) ? undefined : options[key] as string;
  const previous = await readEnvironment($);
  const body = JSON.parse(previous.CLAUDE_CODE_EXTRA_BODY || '{}');
  if (!body || Array.isArray(body) || typeof body !== 'object') {
    throw new Error('CLAUDE_CODE_EXTRA_BODY must be a JSON object.');
  }
  const environment = {
    ENABLE_SMART_ROUTING_V2: enabled ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    CLAUDE_CODE_EXTRA_BODY: JSON.stringify({...body,
      smart_router_recipe_name: enabled ? options.recipe : 'DISABLED'}),
  };
  try {
    await writeEnvironment($, environment);
  } catch (error) {
    // Restore earlier successful writes if a later environment write is denied.
    await writeEnvironment($, previous);
    throw error;
  }
}


export function register(on: On, options: PluginOptions): void {
  const launchedEnabled = options.ENABLE_SMART_ROUTING_V2 === '1' ||
    options.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1';

  on('session.start', async ($, e, next) => {
    if (launchedEnabled) {
      const current = await readEnvironment($);
      await setRouting($, options, current.ENABLE_SMART_ROUTING_V2 === '1' ||
        current.ENABLE_SMART_ROUTING_SUBAGENT_ONLY === '1');
    }
    return next(e);
  });

  // The installed smart-router skill already supplies the slash command.
  on('command.run', {command: 'smart-router'}, async ($, e) => {
    const action = e.args.trim();
    if (action !== 'on' && action !== 'off') return {text: 'Usage: /smart-router on|off'};
    if (!launchedEnabled) return {text: 'Smart Router was not enabled for this session.'};
    try {
      await setRouting($, options, action === 'on');
      const guidance = options.ENABLE_SMART_ROUTER_ORCHESTRATOR !== '1' ? '' : action === 'off'
        ? ' Earlier Smart Router Orchestrator instructions are superseded. Stop automatic delegation; honor explicit subagent requests using native model selection. Existing children can finish.'
        : ' Apply the smart-router-orchestrator skill to further work.';
      return {text: `Smart Router is ${action} for this session.${guidance}`};
    } catch {
      return {text: 'Could not update Smart Router. Check CLAUDE_CODE_EXTRA_BODY and environment permissions; retry or restart through UG.'};
    }
  });
}
