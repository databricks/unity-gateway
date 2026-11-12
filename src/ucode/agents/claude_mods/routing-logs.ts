import type { EngineInterface, On, PluginOptions } from 'claude-code';

const ROUTING_LOGS_PANE = 'smart-router-logs';
type Decision = { at: number; preview: string | null; selected_model: string; routing_ms: number };

const isDecision = (d: Decision): boolean =>
  Boolean(d) && Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
  (d.preview === null || typeof d.preview === 'string') && typeof d.selected_model === 'string' &&
  Number.isFinite(d.routing_ms);

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
function logsReaderCommand(options: PluginOptions): string[] {
  const command = options.logsCommand as string[];
  if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string' || !arg)) {
    throw new Error('logs command unavailable');
  }
  return command;
}

// Run the reader CLI once and store its result. Returns the debug-log note,
// empty when the read succeeded. Local because the mod compiler keeps $ only
// within the file that declares the function receiving it.
async function runRoutingLogsReader($: EngineInterface, options: PluginOptions): Promise<string> {
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
function applyRoutingLogsRead(stdout: string, readError?: string): string {
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

async function load($: EngineInterface, options: PluginOptions): Promise<void> {
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

async function refreshRoutingLogs($: EngineInterface, options: PluginOptions): Promise<void> {
  const open = await $.ui.panes();
  if (!Array.isArray(open) || !open.some((pane) => pane?.id === ROUTING_LOGS_PANE)) {
    paneOpen = false;
    return;
  }
  await load($, options);
}

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  // Match both launch modes to coexist with the routing module's unmatched spawn hook.
  on('agent.spawn', { background: [true, false] }, async ($, e, next) => {
    const result = await next(e);
    if (paneOpen && !e.resume && result.agentId && !result.deny) {
      // Refresh routing logs async to not delay subagent startup.
      void refreshRoutingLogs($, options).catch(() => {});
    }
    return result;
  });

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    if (e.args.trim() !== 'logs') return next(e);
    if (!launchedEnabled) return { text: 'Smart Router was not enabled for this session.' };
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
    const { Box, Text } = $.ui.resolve(e);
    const columns = Number.isFinite(e.props.bodyColumns) ? e.props.bodyColumns : 80;
    if (failed) {
      const children = [Text({ children: ['Could not read Smart Router logs.'] })];
      if (cliError) children.push(Text({ dimColor: true, children: [cliError.slice(0, columns)] }));
      return Box({ flexDirection: 'column', children });
    }
    const children = [Text({ dimColor: true, children: [`${rows.length} routed prompt${rows.length === 1 ? '' : 's'} this session`] })];
    if (!rows.length) children.push(Text({ dimColor: true, children: ['No routed subagents in this session yet.'] }));
    for (const row of rows) {
      const model = row.selected_model.replace(/^system\.ai\./, '').replace(/^databricks-/, '');
      // Age (8) + "  " + preview + "  " + model fills the row, so the model ends at the right edge.
      const room = Math.max(1, columns - model.length - 12);
      const preview = (row.preview || '(prompt not recorded)').replace(/\s+/g, ' ');
      const label = preview.length > room ? preview.slice(0, Math.max(0, room - 1)) + '…' : preview;
      children.push(Box({ flexDirection: 'row', children: [
        Text({ dimColor: true, children: [`${age(row.at).padStart(8)}  `] }),
        Text({ children: [`${label.padEnd(room)}  `] }),
        Text({ color: 'green', bold: true, children: [model] }),
      ] }));
    }
    return Box({ flexDirection: 'column', children });
  });
}
