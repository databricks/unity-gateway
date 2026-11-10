import type { On, PluginOptions } from 'claude-code';
import { shortModel } from './subagent-routing.ts';

const PANE = 'smart-router-logs';
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

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  let rows: Decision[] = [];
  let failed = false;

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    if (e.args.trim() !== 'logs') return next(e);
    if (!launchedEnabled) return { text: 'Smart Router was not enabled for this session.' };
    try {
      const command = options.logsCommand as string[];
      if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string' || !arg)) {
        throw new Error('logs command unavailable');
      }
      const session = await $.session.id();
      const result = await $.process.run([...command, '--session', session], { timeoutMs: 10000 });
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
    const { Box, Text } = $.ui.resolve(e);
    if (failed) return Text({ children: ['Could not read Smart Router logs.'] });
    const children = [Text({ dimColor: true, children: [`${rows.length} routed prompt${rows.length === 1 ? '' : 's'} this session`] })];
    if (!rows.length) children.push(Text({ dimColor: true, children: ['No routed subagents in this session yet.'] }));
    for (const row of rows) {
      const model = shortModel(row.selected_model);
      const columns = Number.isFinite(e.props.bodyColumns) ? e.props.bodyColumns : 80;
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
