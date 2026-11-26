import type { AgentInfo, SessionEndInput } from 'claude-code';

/** Claude requires API calls to stay in registered hooks. Pass their operations
 * to this owner so snapshot ordering stays separate from the hook wiring.
 */
type Source = {
  path?: string;
  read: () => Promise<{ sessionId: string; agents: AgentInfo[] }>;
  write: (path: string, text: string) => Promise<void>;
  every: (tick: () => Promise<void>) => { cancel: () => void };
};

/** A question belongs to one call in one conversation. Keeping its identity
 * separate from whether it was drawn lets completion ignore replaced calls.
 */
type QuestionCall = Readonly<{ sessionId: string; agentId: string }>;

/** Publish registry snapshots independently of rendering or the main turn's status.
 * The launcher owns OSC output. Serial writes ensure a slow scan cannot overwrite
 * a conversation-end snapshot, and failed reads retain the last known status.
 */
export function createSubagentProgramStatus() {
  let source: Source | undefined;
  let endedSession: string | undefined;
  let lastSnapshot = '';
  let scanning = false;
  let pending: Promise<void> = Promise.resolve();
  let timer: { cancel: () => void } | undefined;
  const questions = new Map<string, Readonly<{ call: QuestionCall; shown: boolean }>>();

  const enqueue = (operation: () => Promise<void>): Promise<void> => {
    pending = pending.then(operation).catch(() => undefined);
    return pending;
  };

  const scan = (): Promise<void> => {
    if (scanning || !source?.path) return Promise.resolve();
    scanning = true;
    return enqueue(async () => {
      const current = source!;
      const { sessionId, agents } = await current.read();
      if (sessionId === endedSession) return;
      const asking = new Set<string>();
      for (const [id, question] of questions) {
        if (question.call.sessionId !== sessionId) questions.delete(id);
        else if (question.shown) asking.add(question.call.agentId);
      }
      // Keep each agent's identity and lifecycle state. The terminal, rather
      // than this producer, decides how to combine siblings and the main turn.
      const tasks = agents.map(agent => {
        // A displayed question is stronger evidence than a registry sample that
        // still says Running. Finished agents retain their reported outcome.
        const needsAnswer = asking.has(agent.id)
          && !['completed', 'failed', 'killed'].includes(agent.status);
        return {
          id: agent.id,
          status: needsAnswer ? 'waiting' : agent.status,
          title: agent.description || agent.name || agent.type,
          ...(needsAnswer ? { kind: 'question' } : {}),
        };
      }).sort((a, b) => a.id.localeCompare(b.id));
      const snapshot = JSON.stringify({ sessionId, agents: tasks });
      if (snapshot !== lastSnapshot) {
        await current.write(current.path!, snapshot);
        lastSnapshot = snapshot;
      }
    }).finally(() => { scanning = false; });
  };

  const start = async (current: Source): Promise<void> => {
    source = current;
    if (current.path) {
      endedSession = undefined;
      lastSnapshot = '';
      await scan();
      timer ??= current.every(scan);
    }
  };

  const end = async (e: SessionEndInput): Promise<void> => {
    endedSession = e.sessionId;
    questions.clear();
    if (e.reason !== 'clear' && e.reason !== 'resume') {
      timer?.cancel();
      timer = undefined;
    }
    await enqueue(async () => {
      lastSnapshot = '';
      if (source?.path) await source.write(source.path, JSON.stringify({ sessionId: e.sessionId, agents: null }));
    });
    // /clear starts another conversation without session.start. Keep sampling
    // until session.id changes, and leave the ended conversation cleared.
  };

  /** Track the call before it can open a dialog. Calling the tool alone is not
   * proof of user input: another hook may answer it automatically.
   */
  const beginQuestion = (sessionId: string | undefined, requestId: string, agentId: string): (() => void) => {
    if (!source?.path || !sessionId || sessionId === endedSession) return () => {};
    const call: QuestionCall = { sessionId, agentId };
    questions.set(requestId, { call, shown: false });
    return () => {
      if (questions.get(requestId)?.call === call) questions.delete(requestId);
    };
  };

  /** Observe Claude's actual question dialog without changing its rendering.
   * The regular sampler publishes this change; rendering never performs I/O.
   * A late redraw after completion or conversation reset has no live call.
   */
  const questionShown = (requestId: string): void => {
    const question = questions.get(requestId);
    if (question) questions.set(requestId, { ...question, shown: true });
  };

  return { start, end, beginQuestion, questionShown };
}
