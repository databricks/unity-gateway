import type { On, PluginOptions } from 'claude-code';

const PANE = 'smart-router-logs';
type Decision = { at: number; preview: string | null; selected_model: string; routing_ms: number };

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
      const result = await $.process.run([...command, '--session', session, '--harness', 'claude', '--json'], { timeoutMs: 10000 });
      if (result.exitCode !== 0 || result.isStdoutTruncated) throw new Error(`logs command exited ${result.exitCode} or truncated output`);
      let data;
      try { data = JSON.parse(result.stdout); } catch { throw new Error('invalid logs JSON'); }
      if (data?.v !== 1 || !Array.isArray(data.decisions) || data.decisions.some((d: Decision) =>
        !d || !Number.isFinite(d.at) || !Number.isFinite(new Date(d.at * 1000).getTime()) ||
        (d.preview !== null && typeof d.preview !== 'string') || typeof d.selected_model !== 'string' ||
        !Number.isFinite(d.routing_ms))) throw new Error('invalid logs response');
      rows = data.decisions.slice().reverse();
      failed = false;
    } catch (error) {
      rows = [];
      failed = true;
      $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'read failed'}`, { to: 'debug' });
    }
    try {
      await $.ui.open({ id: PANE, title: 'Smart Router logs', focus: true, closeOnEscape: true });
      $.ui.invalidate('ui.render');
      return { text: 'Smart Router logs opened.' };
    } catch {
      $.ui.log('Smart Router logs: could not open pane', { to: 'debug' });
      return { text: 'Could not read Smart Router logs.' };
    }
  });

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text } = $.ui.resolve(e);
    if (failed) return Text({ children: ['Could not read Smart Router logs.'] });
    const children = [Text({ dimColor: true, children: [`${rows.length} routed prompt${rows.length === 1 ? '' : 's'} this session`] })];
    if (!rows.length) children.push(Text({ dimColor: true, children: ['No routed subagents in this session yet.'] }));
    for (const row of rows) {
      const model = row.selected_model.replace(/^system\.ai\./, '').replace(/^databricks-/, '');
      const seconds = `${(row.routing_ms / 1000).toFixed(1)}s`;
      const room = Math.max(1, e.props.bodyColumns - model.length - seconds.length - 11);
      const preview = (row.preview || '(prompt not recorded)').replace(/\s+/g, ' ');
      const label = preview.length > room ? preview.slice(0, Math.max(0, room - 1)) + '…' : preview;
      children.push(Box({ flexDirection: 'row', children: [
        Text({ dimColor: true, children: [`${new Date(row.at * 1000).toTimeString().slice(0, 5)}  `] }),
        Text({ children: [`${label}  `] }),
        Text({ color: 'green', bold: true, children: [model] }),
        Text({ dimColor: true, children: [`  ${seconds}`] }),
      ] }));
    }
    return Box({ flexDirection: 'column', children });
  });
}
