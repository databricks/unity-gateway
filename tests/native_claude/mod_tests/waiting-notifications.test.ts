import type { AgentInfo, On, PromptSubmitInput } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import { waitingAgent } from '../hooks/waiting-notifications.ts';

// Claude 2.1.316 Auto Mode output, with a fixture task, tool id and path.
const notice = `<task-notification>
<task-id>a123</task-id>
<tool-use-id>toolu_watch</tool-use-id>
<output-file>/tmp/watcher.output</output-file>
<status>completed</status>
<summary>Agent "Watch CI" is waiting on its own background work</summary>
<note>This agent stopped with background work of its own still running. It may resume on its own when that work completes or reports, and the same task-id notifies again if it does; the result below may be interim.</note>
<result>This agent has not reported yet: it is waiting on its own background work and will deliver its report through SubagentHandback when that finishes.
</result>
<usage><subagent_tokens>125</subagent_tokens><tool_uses>1</tool_uses><duration_ms>190</duration_ms></usage>
</task-notification>`;
const prompt: PromptSubmitInput = {text: notice, wait: false, origin: {kind: 'task-notification'}};
const agent: AgentInfo = {id: 'a123', description: 'Watch CI', type: 'general-purpose', status: 'waiting'};

test('only the empty handback for a currently waiting direct child is passive', () => {
  expect(waitingAgent(prompt, [agent])).toEqual(agent);
  expect(waitingAgent(prompt, [])).toBeUndefined();
  for (const status of ['pending', 'running', 'idle', 'completed', 'failed', 'killed'] as const) {
    expect(waitingAgent(prompt, [{...agent, status}])).toBeUndefined();
  }
  for (const other of [{id: 'a456'}, {parentId: 'parent'}, {description: 'Different task'}]) {
    expect(waitingAgent(prompt, [{...agent, ...other}])).toBeUndefined();
  }
});

test('typed, peer, scheduled and plugin text cannot masquerade as waiting notices', () => {
  for (const kind of ['composer', 'sdk', 'peer', 'peer-send-message', 'scheduled-trigger', 'unclassified'] as const) {
    expect(waitingAgent({...prompt, origin: {kind}}, [agent])).toBeUndefined();
  }
  expect(waitingAgent({...prompt, origin: {kind: 'plugin', name: 'other'}}, [agent])).toBeUndefined();
  expect(waitingAgent({text: notice, wait: false} as PromptSubmitInput, [agent])).toBeUndefined();
  expect(waitingAgent({...prompt, context: ['Needs attention']}, [agent])).toBeUndefined();
  expect(waitingAgent({...prompt, attachments: [{type: 'image', mediaType: 'image/png'}]}, [agent])).toBeUndefined();
});

test('reports, warnings, failures, mixed deliveries and unfamiliar formats stay actionable', () => {
  for (const text of [
    notice.replace('<status>completed', '<status>failed'),
    notice.replace('is waiting on its own background work</summary>', 'completed</summary>'),
    // Claude 2.1.290 formats the same empty handback with a "finished" summary.
    notice.replace('is waiting on its own background work</summary>', 'finished</summary>'),
    notice.replace('<result>', '<result>Safety review unavailable. '),
    notice.replace(/<result>[\s\S]*?<\/result>/, '<result>I need access to continue.</result>'),
    notice.replace('the result below may be interim.', 'the result below may be interim. Unread messages remain.'),
    notice.replace('SubagentHandback', 'A different reporting tool'),
    notice.replace('<task-id>a123</task-id>', '<task-id>a123</task-id>\n<task-id>a456</task-id>'),
    `${notice}\nPlease review this failure.`,
    `${notice}\n\n${notice}`,
  ]) expect(waitingAgent({...prompt, text}, [agent])).toBeUndefined();
});

test('optional metadata and escaped descriptions keep the same child identity', () => {
  const text = notice.replace(/<tool-use-id>.*<\/tool-use-id>\n/, '')
    .replace(/<output-file>.*<\/output-file>\n/, '')
    .replace(/\n<usage>.*<\/usage>/, '')
    .replace('Watch CI', 'Watch A &amp; B &lt;CI&gt;');
  const escaped = {...agent, description: 'Watch A & B <CI>'};
  expect(waitingAgent({...prompt, text}, [escaped])).toEqual(escaped);
});

const routed = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  orchestratorInstructions: 'Delegate independent work.',
};

// Only the host's environment, task lookup and delivery boundary are controlled.
// The plugin's registered prompt.submit callback and status band run unchanged.
function deliveryHost(on: On, env: Record<string, string>, agents: () => AgentInfo[]) {
  mock.clock(on);
  on('ui.render', {component: 'AbovePrompt'}, ($, e) => $.ui.resolve(e).Text({children: ['native']}));
  on('turn.complete', () => ({text: ''}));
  const delivered: PromptSubmitInput[] = [];
  on('env.get', ($, e) => ({value: env[e.name]}));
  on('agent.list', () => ({value: agents()}));
  on('prompt.submit', ($, e) => {
    delivered.push(e);
    return {text: e.text};
  });
  return delivered;
}

const bandSlot = {plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {}} as const;
const hasBand = (tree: unknown) => JSON.stringify(tree).includes('unity gateway smart router');

test('registered submit drops only the empty notice without arming the band', {options: routed}, async ($, on) => {
  const child = {...agent};
  const delivered = deliveryHost(on, routed, () => [child]);
  const band = await $.ui.mount(bandSlot);
  expect(hasBand(await band.drawn())).toBe(true);
  expect(await $.prompt.submit(prompt)).toEqual({drop: 'Watch CI is still running in the background.'});
  expect(delivered).toEqual([]);
  await $.turn.complete({answer: ''});
  expect(hasBand(await band.drawn())).toBe(true);

  child.status = 'completed';
  const report = {...prompt, text: notice.replace(/<result>[\s\S]*?<\/result>/, '<result>Finished the report.</result>')};
  expect(await $.prompt.submit(report)).toEqual({text: report.text});
  expect(delivered).toEqual([report]);
  await $.turn.complete({answer: 'Read the report.'});
  expect(hasBand(await band.drawn())).toBe(false);
});

for (const key of ['ENABLE_SMART_ROUTER_ORCHESTRATOR', 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY']) {
  test(`registered submit delivers waiting notices with ${key} off`, {options: routed}, async ($, on) => {
    const env = {...routed, [key]: '0'};
    const delivered = deliveryHost(on, env, () => [agent]);
    const band = await $.ui.mount(bandSlot);
    expect(hasBand(await band.drawn())).toBe(true);
    expect(await $.prompt.submit(prompt)).toEqual({text: notice});
    expect(delivered).toEqual([prompt]);
    await $.turn.complete({answer: ''});
    expect(hasBand(await band.drawn())).toBe(false);
  });
}

test('registered submit preserves delivery when task lookup fails', {options: routed}, async ($, on) => {
  const delivered = deliveryHost(on, routed, () => {throw new Error('Task state unavailable');});
  expect(await $.prompt.submit(prompt)).toEqual({text: notice});
  expect(delivered).toEqual([prompt]);
});
