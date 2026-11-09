import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { shortModel } from './subagent-routing.ts';

const PANE = 'smart-router-logs';
type Decision = {
  decision_id: string;
  at: number;
  preview: string | null;
  selected_model: string;
  routing_ms: number;
};
type Detail = {
  id: string;
  failed: boolean;
  preview: string | null;
  promptLength: number | null;
  promptHash: string | null;
  subagent: string | null;
  selected: string;
  requested: string | null;
  routing_ms: number;
  rationale: string | null;
  candidates: string[];
};

function logsCommand(options: PluginOptions): string[] {
  const command = options.logsCommand as string[];
  if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string' || !arg)) {
    throw new Error('logs command unavailable');
  }
  return command;
}

// A v1 decision record: ignores unknown keys (a later `explanation`, for one).
function parseDecision(raw: string, id: string): Detail {
  let data: { v?: unknown; decision?: unknown } | null;
  try { data = JSON.parse(raw); } catch { throw new Error('invalid decision JSON'); }
  const d = data?.decision as Record<string, unknown> | null | undefined;
  const prompt = d?.prompt as { preview?: unknown; sha256?: unknown; length?: unknown } | null | undefined;
  const original = d?.original as { subagent_type?: unknown } | null | undefined;
  const candidates = Array.isArray(d?.candidates) ? d.candidates : [];
  const validPrompt = prompt == null || (typeof prompt === 'object' && !Array.isArray(prompt) &&
    (prompt.preview === null || typeof prompt.preview === 'string') &&
    typeof prompt.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(prompt.sha256) &&
    typeof prompt.length === 'number' && Number.isInteger(prompt.length) && prompt.length >= 0);
  const validOriginal = original == null || (typeof original === 'object' && !Array.isArray(original) &&
    (original.subagent_type === null || typeof original.subagent_type === 'string'));
  const validCandidates = Array.isArray(d?.candidates) && candidates.every(candidate =>
    candidate !== null && typeof candidate === 'object' && !Array.isArray(candidate) &&
    typeof (candidate as { model?: unknown }).model === 'string');
  if (data?.v !== 1 || !d || d.decision_id !== id || !validPrompt || !validOriginal ||
    typeof d.selected_model !== 'string' || !Number.isFinite(d.routing_ms) ||
    (d.requested_model !== null && typeof d.requested_model !== 'string') ||
    (d.rationale !== null && typeof d.rationale !== 'string') || !validCandidates) {
    throw new Error('invalid decision response');
  }
  return {
    id,
    failed: false,
    preview: typeof prompt?.preview === 'string' ? prompt.preview : null,
    promptLength: typeof prompt?.length === 'number' ? prompt.length : null,
    promptHash: typeof prompt?.sha256 === 'string' ? prompt.sha256.slice(0, 12) : null,
    // UG's own route agents (`ucode-route-<model>`) name a model, not a task type, and
    // would contradict the selected model, so only other subagent types are shown.
    subagent: typeof original?.subagent_type === 'string' && !/(^|:)ucode-route-/.test(original.subagent_type)
      ? original.subagent_type : null,
    selected: d.selected_model,
    requested: typeof d.requested_model === 'string' ? d.requested_model : null,
    routing_ms: d.routing_ms,
    rationale: typeof d.rationale === 'string' ? d.rationale : null,
    candidates: candidates.map(candidate => (candidate as { model: string }).model),
  };
}

function failedDetail(id: string): Detail {
  return {
    id, failed: true, preview: null, promptLength: null, promptHash: null,
    subagent: null, selected: '', requested: null, routing_ms: 0, rationale: null, candidates: [],
  };
}

async function openDecision($: EngineInterface, options: PluginOptions, id: string): Promise<Detail> {
  try {
    const result = await $.process.run(
      [...logsCommand(options), '--session', await $.session.id(), '--decision', id],
      { timeoutMs: 10000 },
    );
    if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error(`decision command exited ${result.exitCode} or truncated output`);
    return parseDecision(result.stdout, id);
  } catch (error) {
    $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'load failed'}`, { to: 'debug' });
    return failedDetail(id);
  }
}

const isDecision = (d: Decision): boolean =>
  Boolean(d) && typeof d.decision_id === 'string' && Boolean(d.decision_id) &&
  Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
  (d.preview === null || typeof d.preview === 'string') && typeof d.selected_model === 'string' &&
  Number.isFinite(d.routing_ms);

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  let rows: Decision[] = [];
  let failed = false;
  let detail: Detail | null = null;
  let detailRequest = 0;

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    if (e.args.trim() !== 'logs') return next(e);
    if (!launchedEnabled) return { text: 'Smart Router was not enabled for this session.' };
    detailRequest++;
    detail = null;
    try {
      const session = await $.session.id();
      const result = await $.process.run([...logsCommand(options), '--session', session], { timeoutMs: 10000 });
      if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error(`logs command exited ${result.exitCode} or truncated output`);
      let data;
      try { data = JSON.parse(result.stdout); } catch { throw new Error('invalid logs JSON'); }
      if (data?.v !== 1 || !Array.isArray(data.decisions)) throw new Error('invalid logs response');
      rows = data.decisions.filter(isDecision).reverse();
      failed = false;
    } catch (error) {
      rows = [];
      failed = true;
      $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'read failed'}`, { to: 'debug' });
    }
    try {
      const pane = await $.ui.open({ id: PANE, title: 'Smart Router logs', focus: true, closeOnEscape: true });
      if (!pane.isPlaced) {
        await $.ui.close({ id: PANE });
        throw new Error('pane was not placed');
      }
      $.ui.invalidate('ui.render');
      return { text: 'Smart Router logs opened.' };
    } catch {
      $.ui.log('Smart Router logs: could not open pane', { to: 'debug' });
      return { text: 'Could not open the Smart Router logs pane.' };
    }
  });

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e);
    if (detail) {
      const children = [
        Button({
          key: 'back', plain: true, autoFocus: true, children: ['← Back'],
          onPress: () => { detailRequest++; detail = null; $.ui.invalidate('ui.render'); },
        }),
      ];
      if (detail.failed) {
        children.push(Text({ children: ['Could not load this routing decision.'] }));
      } else {
        children.push(Text({ dimColor: true, children: [detail.subagent ? `Prompt preview (${detail.subagent})` : 'Prompt preview'] }));
        children.push(Text({ children: [detail.preview ?? '(prompt not recorded)'] }));
        if (detail.promptLength !== null) {
          const hash = detail.promptHash ? ` · SHA-256: ${detail.promptHash}` : '';
          children.push(Text({ dimColor: true, children: [`Length: ${detail.promptLength} characters${hash}`] }));
        }
        children.push(Text({ dimColor: true, children: ['Full prompt is not stored.'] }));
        const seconds = `${(detail.routing_ms / 1000).toFixed(1)}s`;
        children.push(Box({ flexDirection: 'row', children: [
          Text({ children: ['Selected: '] }),
          Text({ color: 'green', bold: true, children: [shortModel(detail.selected)] }),
          Text({ dimColor: true, children: [`  ${seconds}`] }),
        ] }));
        if (detail.requested !== null && detail.requested !== detail.selected) {
          children.push(Text({ dimColor: true, children: [`Requested: ${shortModel(detail.requested)}`] }));
        }
        children.push(Text({ dimColor: true, children: ['Rationale'] }));
        children.push(Text({ children: [detail.rationale?.trim() ? detail.rationale : 'No rationale returned.'] }));
        children.push(Text({ dimColor: true, children: ['Candidates'] }));
        for (const candidate of detail.candidates) {
          children.push(Text({ children: [`${candidate === detail.selected ? '● ' : '○ '}${shortModel(candidate)}`] }));
        }
      }
      return Box({ flexDirection: 'column', children });
    }
    if (failed) return Text({ children: ['Could not read Smart Router logs.'] });
    const children = [Text({ dimColor: true, children: [`${rows.length} routed prompt${rows.length === 1 ? '' : 's'} this session`] })];
    if (!rows.length) children.push(Text({ dimColor: true, children: ['No routed subagents in this session yet.'] }));
    for (const row of rows) {
      const model = shortModel(row.selected_model);
      const columns = Number.isFinite(e.props.bodyColumns) ? e.props.bodyColumns : 80;
      const room = Math.max(1, columns - model.length - 4);
      const preview = (row.preview || '(prompt not recorded)').replace(/\s+/g, ' ');
      const label = preview.length > room ? preview.slice(0, Math.max(0, room - 1)) + '…' : preview;
      children.push(Button({
        key: row.decision_id, plain: true,
        onPress: async () => {
          const request = ++detailRequest;
          const loaded = await openDecision($, options, row.decision_id);
          if (request !== detailRequest) return;
          detail = loaded;
          $.ui.invalidate('ui.render');
        },
        children: [
          `${label}  `,
          Text({ color: 'green', bold: true, children: [model] }),
        ],
      }));
    }
    return Box({ flexDirection: 'column', children });
  });
}
