import type { EngineInterface, On, PluginOptions } from 'claude-code';

export const ROUTING_LOGS_PANE = 'smart-router-logs';
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
  prompt: string;
  subagent: string | null;
  selected: string;
  requested: string | null;
  routing_ms: number;
  rationale: string | null;
  candidates: string[];
};

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '');

const isDecision = (d: Decision): boolean =>
  Boolean(d) && typeof d.decision_id === 'string' && Boolean(d.decision_id) &&
  Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
  (d.preview === null || typeof d.preview === 'string') && typeof d.selected_model === 'string' &&
  Number.isFinite(d.routing_ms);

// A v1 decision record: ignores unknown keys (a later `explanation`, for one).
function parseDecision(raw: string, id: string): Detail {
  let data: { v?: unknown; decision?: unknown } | null;
  try { data = JSON.parse(raw); } catch { throw new Error('invalid decision JSON'); }
  const d = data?.decision as Record<string, unknown> | null | undefined;
  const prompt = d?.prompt as { preview?: unknown } | null | undefined;
  const original = d?.original as { subagent_type?: unknown } | null | undefined;
  const candidates = Array.isArray(d?.candidates) ? (d.candidates as unknown[]) : null;
  if (data?.v !== 1 || !d || d.decision_id !== id ||
    (prompt !== null && prompt !== undefined && (typeof prompt !== 'object' || (prompt.preview !== null && typeof prompt.preview !== 'string'))) ||
    (original !== null && original !== undefined && (typeof original !== 'object' || (original.subagent_type !== null && typeof original.subagent_type !== 'string'))) ||
    typeof d.selected_model !== 'string' || !Number.isFinite(d.routing_ms) ||
    (d.requested_model !== null && typeof d.requested_model !== 'string') ||
    (d.rationale !== null && typeof d.rationale !== 'string') || !candidates ||
    candidates.some(c => !c || typeof (c as { model?: unknown }).model !== 'string')) {
    throw new Error('invalid decision response');
  }
  return {
    id,
    failed: false,
    prompt: typeof prompt?.preview === 'string' ? prompt.preview : '(prompt not recorded)',
    subagent: typeof original?.subagent_type === 'string' ? original.subagent_type : null,
    selected: d.selected_model,
    requested: typeof d.requested_model === 'string' ? d.requested_model : null,
    routing_ms: d.routing_ms,
    rationale: typeof d.rationale === 'string' ? d.rationale : null,
    candidates: candidates.map(c => (c as { model: string }).model),
  };
}

function failedDetail(id: string): Detail {
  return {
    id, failed: true, prompt: '', subagent: null, selected: '', requested: null, routing_ms: 0, rationale: null, candidates: [],
  };
}

async function openDecision($: EngineInterface, options: PluginOptions, id: string): Promise<Detail> {
  try {
    const result = await $.process.run(
      [...logsReaderCommand(options), '--session', await $.session.id(), '--decision', id],
      { timeoutMs: 10000 },
    );
    if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error(`decision command exited ${result.exitCode} or truncated output`);
    return parseDecision(result.stdout, id);
  } catch (error) {
    $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'load failed'}`, { to: 'debug' });
    return failedDetail(id);
  }
}

// Relative age avoids showing a wall-clock time in an unknown time zone.
function age(at: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - at));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

let rows: Decision[] = [];
let failed = false;
// The reader CLI's own error hint, when its failure carries one.
let cliError: string | undefined;
// Whether the logs pane has been placed and has not been seen closed since.
let paneOpen = false;
// One reader at a time; a load asked for while one runs reruns after it finishes.
let loading = false;
let rerun = false;

// The reader command, validated: an unconfigured or malformed logsCommand fails here.
export function logsReaderCommand(options: PluginOptions): string[] {
  const command = options.logsCommand as string[];
  if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string' || !arg)) {
    throw new Error('logs command unavailable');
  }
  return command;
}

// Run the reader CLI once and store its result. Returns the debug-log note,
// empty when the read succeeded. Local because the mod compiler keeps $ only
// within the file that declares the function receiving it.
async function runRoutingLogsReader($: any, options: PluginOptions): Promise<string> {
  try {
    const command = logsReaderCommand(options);
    const session = await $.session.id();
    const result = await $.process.run([...command, '--session', session], { timeoutMs: 10000 });
    return applyRoutingLogsRead(result.stdout,
      result.exitCode !== 0 || result.isStdoutTruncated
        ? `logs command exited ${result.exitCode} or truncated output`
        : undefined);
  } catch (error) {
    return applyRoutingLogsRead('', error instanceof Error ? error.message : 'read failed');
  }
}

// Store one reader result in the pane state. A failed or malformed read shows the generic
// notice; a CLI error envelope {"v":1,"error":"..."} adds its hint under that notice.
// Returns the debug-log note, empty when the read succeeded.
export function applyRoutingLogsRead(stdout: string, readError?: string): string {
  cliError = undefined;
  if (readError !== undefined) {
    try {
      const envelope = JSON.parse(stdout);
      if (typeof envelope?.error === 'string' && envelope.error.trim()) {
        cliError = envelope.error.trim().slice(0, 200);
      }
    } catch {}
    rows = [];
    failed = true;
    return `Smart Router logs: ${readError}`;
  }
  try {
    let data;
    try { data = JSON.parse(stdout); } catch { throw new Error('invalid logs JSON'); }
    if (data?.v !== 1 || !Array.isArray(data.decisions)) throw new Error('invalid logs response');
    rows = data.decisions.filter(isDecision).reverse();
    failed = false;
    return '';
  } catch (error) {
    rows = [];
    failed = true;
    return `Smart Router logs: ${error instanceof Error ? error.message : 'read failed'}`;
  }
}

export function routingLogsPaneOpen(): boolean {
  return paneOpen;
}

export function routingLogsPaneClosed(): void {
  paneOpen = false;
}

async function load($: any, options: PluginOptions): Promise<void> {
  if (loading) {
    rerun = true;
    return;
  }
  loading = true;
  try {
    const note = await runRoutingLogsReader($, options);
    if (note) $.ui.log(note, { to: 'debug' });
  } finally {
    loading = false;
  }
  if (rerun) {
    rerun = false;
    await load($, options);
  }
  $.ui.invalidate('ui.render');
}

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  let detail: Detail | null = null;

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    if (e.args.trim() !== 'logs') return next(e);
    if (!launchedEnabled) return { text: 'Smart Router was not enabled for this session.' };
    detail = null;
    await load($, options);
    try {
      const pane = await $.ui.open({ id: ROUTING_LOGS_PANE, title: 'Smart Router logs', focus: true, closeOnEscape: true });
      if (!pane.isPlaced) {
        await $.ui.close({ id: ROUTING_LOGS_PANE });
        throw new Error('pane was not placed');
      }
      paneOpen = true;
      $.ui.invalidate('ui.render');
      return { text: 'Smart Router logs opened.' };
    } catch {
      $.ui.log('Smart Router logs: could not open pane', { to: 'debug' });
      return { text: 'Could not open the Smart Router logs pane.' };
    }
  });

  on('ui.render', { component: 'Pane', requestId: ROUTING_LOGS_PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e);
    const columns = Number.isFinite(e.props.bodyColumns) ? e.props.bodyColumns : 80;
    if (detail) {
      const children = [
        Button({
          key: 'back', plain: true, autoFocus: true, children: ['← Back'],
          onPress: () => { detail = null; $.ui.invalidate('ui.render'); },
        }),
      ];
      if (detail.failed) {
        children.push(Text({ children: ['Could not load this routing decision.'] }));
      } else {
        children.push(Text({ dimColor: true, children: [detail.subagent ? `Prompt (${detail.subagent})` : 'Prompt'] }));
        children.push(Text({ children: [detail.prompt] }));
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
        children.push(Text({ children: [detail.rationale ?? 'No rationale returned.'] }));
        children.push(Text({ dimColor: true, children: ['Candidates'] }));
        for (const candidate of detail.candidates) {
          children.push(Text({ children: [`${candidate === detail.selected ? '● ' : '○ '}${shortModel(candidate)}`] }));
        }
      }
      return Box({ flexDirection: 'column', children });
    }
    if (failed) {
      const children = [Text({ children: ['Could not read Smart Router logs.'] })];
      if (cliError) children.push(Text({ dimColor: true, children: [cliError.slice(0, columns)] }));
      return Box({ flexDirection: 'column', children });
    }
    const children = [Text({ dimColor: true, children: [`${rows.length} routed prompt${rows.length === 1 ? '' : 's'} this session`] })];
    if (!rows.length) children.push(Text({ dimColor: true, children: ['No routed subagents in this session yet.'] }));
    for (const row of rows) {
      const model = shortModel(row.selected_model);
      // Age (8) + "  " + preview + "  " + model fills the row, so the model ends at the right edge.
      const room = Math.max(1, columns - model.length - 12);
      const preview = (row.preview || '(prompt not recorded)').replace(/\s+/g, ' ');
      const label = preview.length > room ? preview.slice(0, Math.max(0, room - 1)) + '…' : preview;
      children.push(Button({
        key: row.decision_id, plain: true,
        onPress: async () => {
          detail = await openDecision($, options, row.decision_id);
          $.ui.invalidate('ui.render');
        },
        children: [
          `${age(row.at).padStart(8)}  `,
          `${label.padEnd(room)}  `,
          Text({ color: 'green', bold: true, children: [model] }),
        ],
      }));
    }
    return Box({ flexDirection: 'column', children });
  });
}
