import type { Register } from 'claude-code';
import { register as router } from '../hooks/register';

export const register: Register = (on, options) => {
  router(on, options);
  on('command.run', {command: 'routing-probe'}, async ($) => ({text: JSON.stringify({
    revision: 'initial', options,
    environment: {
      ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2') ?? null,
      ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') ?? null,
      ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR') ?? null,
    },
    body: JSON.parse(await $.env.get('CLAUDE_CODE_EXTRA_BODY') || '{}'),
  })}));
};
