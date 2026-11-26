import type { AgentInfo, PromptSubmitInput } from 'claude-code';

// Only the host's empty handback is passive. An interim report or safety warning
// can need attention even while the same agent still has background work.
const WAITING_RESULT = 'This agent has not reported yet: it is waiting on its own background work and will deliver its report through SubagentHandback when that finishes.\n';
const WAITING_NOTE = 'This agent stopped with background work of its own still running. It may resume on its own when that work completes or reports, and the same task-id notifies again if it does; the result below may be interim.';
const WAITING_NOTICE = /^<task-notification>\n<task-id>([a-zA-Z0-9_-]+)<\/task-id>\n(?:<tool-use-id>[^<>\n]+<\/tool-use-id>\n)?(?:<output-file>[^<>\n]*<\/output-file>\n)?<status>completed<\/status>\n<summary>([^<>\n]+)<\/summary>\n<note>([^<>\n]+)<\/note>\n<result>([^<>]*)<\/result>(?:\n<usage><subagent_tokens>\d+<\/subagent_tokens><tool_uses>\d+<\/tool_uses><duration_ms>\d+<\/duration_ms><\/usage>)?\n<\/task-notification>$/;

export function waitingAgent(
  prompt: PromptSubmitInput,
  agents: readonly AgentInfo[],
): AgentInfo | undefined {
  if (prompt.origin?.kind !== 'task-notification' || prompt.attachments?.length || prompt.context?.length) return;
  const match = WAITING_NOTICE.exec(prompt.text);
  if (!match || match[3] !== WAITING_NOTE || match[4] !== WAITING_RESULT) return;
  const agent = agents.find(({ id, parentId, status }) =>
    id === match[1] && parentId === undefined && status === 'waiting');
  if (!agent) return;
  const summary = `Agent "${agent.description}" is waiting on its own background work`
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  if (match[2] === summary) return agent;
}
