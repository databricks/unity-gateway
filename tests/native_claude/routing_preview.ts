import { registerStatusBand } from './smart-routing-status.ts';
import type { SubagentRoutingState } from './subagent-routing.ts';

export const register = (on) => {
  const subagents: SubagentRoutingState = {records: new Map()};
  registerStatusBand(on, subagents);
  on('session.start', {isInteractive: true}, async ($, event, next) => {
    await $.command.register({name: 'routing-card-preview', description: 'Preview routing cards without inference', immediate: true});
    return next(event);
  });
  on('command.run', {command: 'routing-card-preview'}, ($) => {
    for (const [task, seconds] of [['Compute 1+1 (agent B)', 2.8], ['Compute 1+1 (agent A)', 3.6]] as const) {
      subagents.records.set(`toolu_preview_${seconds}`, {
        task, seconds, model: 'claude-haiku-5-5',
        rationale: 'The task requires only a direct arithmetic operation and a strictly constrained numeric response, with no external information or tools. Selected claude-haiku-5-5 based on predicted quality and cost.',
      });
    }
    $.ui.invalidate('ui.render');
    return {text: 'Preview only; no model request.'};
  });
};
