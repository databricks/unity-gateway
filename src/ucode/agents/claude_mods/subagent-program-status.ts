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
      // Waiting also includes an agent held on its own background work. It
      // does not prove a human is needed, so never invent a blocked report.
      const activeTasks = agents.filter(agent =>
        ['pending', 'running', 'waiting'].includes(agent.status)).length;
      const snapshot = JSON.stringify({ sessionId, activeTasks });
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
    if (e.reason !== 'clear' && e.reason !== 'resume') {
      timer?.cancel();
      timer = undefined;
    }
    await enqueue(async () => {
      lastSnapshot = '';
      if (source?.path) await source.write(source.path, JSON.stringify({ sessionId: e.sessionId, activeTasks: null }));
    });
    // /clear starts another conversation without session.start. Keep sampling
    // until session.id changes, and leave the ended conversation cleared.
  };

  return { start, end };
}
