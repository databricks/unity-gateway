import type { AgentInfo, PromptSubmitInput } from 'claude-code';
import { expect, test } from 'claude-code/testing';
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
