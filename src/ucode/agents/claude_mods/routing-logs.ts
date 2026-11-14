import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { parseRoutingExplanation, type RoutingExplanation } from './subagent-routing.ts';

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
  // The reader CLI's own error hint, when its failure carries one.
  hint?: string;
  preview: string | null;
  subagent: string | null;
  selected: string;
  routing_ms: number;
  rationale: string | null;
  candidates: string[];
  explanation?: RoutingExplanation;
  explanationModel?: string;
  showNumbers?: boolean;
};

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '');

const isDecision = (d: Decision): boolean =>
  Boolean(d) && typeof d.decision_id === 'string' && Boolean(d.decision_id) &&
  Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
  (d.preview === null || typeof d.preview === 'string') && typeof d.selected_model === 'string' &&
  Number.isFinite(d.routing_ms);

// A v1 decision record. Optional explanation details are independently validated so
// malformed metadata never hides the selected route.
function parseDecision(raw: string, id: string): Detail {
  let data: { v?: unknown; decision?: unknown } | null;
  try { data = JSON.parse(raw); } catch { throw new Error('invalid decision JSON'); }
  const d = data?.decision as Record<string, unknown> | null | undefined;
  const prompt = d?.prompt as { preview?: unknown } | null | undefined;
  const original = d?.original as { subagent_type?: unknown } | null | undefined;
  const candidates = Array.isArray(d?.candidates) ? d.candidates : [];
  const validPrompt = prompt == null || (typeof prompt === 'object' && !Array.isArray(prompt) &&
    (prompt.preview === null || typeof prompt.preview === 'string'));
  const validOriginal = original == null || (typeof original === 'object' && !Array.isArray(original) &&
    (original.subagent_type === null || typeof original.subagent_type === 'string'));
  const validCandidates = Array.isArray(d?.candidates) && candidates.every(candidate =>
    candidate !== null && typeof candidate === 'object' && !Array.isArray(candidate) &&
    typeof (candidate as { model?: unknown }).model === 'string');
  if (data?.v !== 1 || !d || d.decision_id !== id || !validPrompt || !validOriginal ||
    typeof d.selected_model !== 'string' || !Number.isFinite(d.routing_ms) ||
    (d.rationale !== null && typeof d.rationale !== 'string') || !validCandidates) {
    throw new Error('invalid decision response');
  }
  const offeredModels = candidates.map(candidate => (candidate as { model: string }).model);
  const explanation = d.explanation === undefined ? undefined : parseRoutingExplanation(
    d.explanation,
    d.selected_model,
    typeof d.session_id === 'string' ? d.session_id : undefined,
    typeof d.tool_use_id === 'string' ? d.tool_use_id : undefined,
    offeredModels,
  );
  return {
    id,
    failed: false,
    preview: typeof prompt?.preview === 'string' ? prompt.preview : null,
    // UG's own route agents (`ucode-route-<model>`) name a model, not a task type, and
    // would contradict the selected model, so only other subagent types are shown.
    subagent: typeof original?.subagent_type === 'string' && !/(^|:)ucode-route-/.test(original.subagent_type)
      ? original.subagent_type : null,
    selected: d.selected_model,
    routing_ms: d.routing_ms,
    rationale: typeof d.rationale === 'string' ? d.rationale : null,
    candidates: offeredModels,
    explanation,
    explanationModel: explanation?.selectedModel,
    showNumbers: false,
  };
}

function failedDetail(id: string): Detail {
  return {
    id, failed: true, preview: null,
    subagent: null, selected: '', routing_ms: 0, rationale: null, candidates: [],
  };
}

async function openDecision($: EngineInterface, options: PluginOptions, id: string): Promise<Detail> {
  let result: { exitCode: number; stdout: string; isStdoutTruncated?: boolean };
  try {
    result = await $.process.run(
      [...logsReaderCommand(options), '--session', await $.session.id(), '--decision', id],
      { timeoutMs: 10000 },
    );
  } catch (error) {
    $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'load failed'}`, { to: 'debug' });
    return failedDetail(id);
  }
  try {
    if (result.exitCode !== 0 || result.isStdoutTruncated) {
      throw new Error(`decision command exited ${result.exitCode} or truncated output`);
    }
    return parseDecision(result.stdout, id);
  } catch (error) {
    $.ui.log(`Smart Router logs: ${error instanceof Error ? error.message : 'load failed'}`, { to: 'debug' });
    const detail = failedDetail(id);
    // A failed load is reported as {"v":1,"error":"..."}; surface that hint under the notice.
    try {
      const envelope = JSON.parse(result.stdout);
      if (typeof envelope?.error === 'string' && envelope.error.trim()) {
        detail.hint = envelope.error.trim().slice(0, 200);
      }
    } catch {}
    return detail;
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

// Refresh an open pane's rows after a spawn, before the turn finishes. A pane that is
// no longer placed is marked closed so neither refresh path keeps reading for it.
async function refreshOpenPane($: any, options: PluginOptions): Promise<void> {
  const open = await $.ui.panes();
  if (!Array.isArray(open) || !open.some((pane) => pane?.id === ROUTING_LOGS_PANE)) {
    paneOpen = false;
    return;
  }
  await load($, options);
}

function estimatedCostText(value: number | undefined): string | undefined {
  if (value === undefined) return undefined;
  const amount = value > 0 && value < 1e-6 ? '<0.000001' : value.toFixed(6).replace(/\.?0+$/, '');
  return `Est. cost: ${amount} USD`;
}

function breakdownText(label: string, values: Record<string, number> | undefined): string | undefined {
  if (!values || !Object.keys(values).length) return undefined;
  return `${label}: ${Object.entries(values).map(([key, value]) => `${key} ${value.toFixed(3)}`).join(' · ')}`;
}

function explanationSection(
  $: any, e: any, current: Detail,
  onCompare: (id: string) => void, onToggleNumbers: () => void,
) {
  const { Box, Text, Button, Select } = $.ui.resolve(e);
  const children = [Text({ bold: true, children: ['Routing explanation'] })];
  if (!current.explanation) {
    children.push(Text({ children: ['No detailed routing explanation was recorded.'] }));
  } else {
    const explanation = current.explanation;
    const chosen = explanation.models.find((model) => model.selected)!;
    const compared = explanation.models.find((model) => model.id === current.explanationModel)
      ?? chosen;
    const numberText = (label: string, value: number | undefined) => value === undefined
      ? undefined : Text({ children: [`${label}: ${value.toFixed(3)}`] });
    const cost = estimatedCostText(compared.estimatedCostUsd);
    const detailLines = [
      numberText('Quality estimate', compared.qualityEstimate),
      cost === undefined ? undefined : Text({ children: [cost] }),
      numberText('Routing score', compared.routingScore),
      ...[
        breakdownText('Components', compared.components),
        breakdownText('Vs. routed', compared.differenceFromWinner),
      ].filter((text) => text !== undefined).map((text) => Text({ dimColor: true, children: [text] })),
    ].filter(Boolean);
    children.push(Text({ color: 'green', bold: true, children: [
      `Chosen: ${chosen.name}`,
    ] }));
    children.push(Text({ children: [explanation.summary] }));
    if (explanation.taskUnderstanding.length) {
      children.push(Text({ bold: true, children: ['Task understanding'] }));
      for (const item of explanation.taskUnderstanding) children.push(Text({ children: [item] }));
    }
    children.push(Box({ marginTop: 1, marginBottom: 1, children: [Select({
      key: 'compare-model',
      label: 'Compare model',
      value: compared.id,
      options: explanation.models.map((model) => ({
        value: model.id,
        label: `${model.name}${model.selected ? ' · chosen' : ''}`,
      })),
      onSelect: onCompare,
    })] }));
    children.push(Text({ bold: true, children: [
      `${compared.name} — ${compared.considered ? 'considered' : 'not considered'}`,
    ] }));
    children.push(Text({ children: [compared.reason] }));
    if (detailLines.length || explanation.formula) {
      children.push(Button({
        key: 'numbers',
        label: current.showNumbers ? 'Hide numbers' : 'Show numbers',
        onPress: onToggleNumbers,
      }));
      if (current.showNumbers) children.push(...detailLines);
      if (current.showNumbers && explanation.formula) {
        children.push(Text({ dimColor: true, children: [`Scoring formula: ${explanation.formula}`] }));
      }
    }
  }
  return children;
}

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  // The open decision and its request token: a newer open or a logs rerun retires a load.
  let detail: Detail | null = null;
  let detailRequest = 0;

  // Match both launch modes to coexist with the routing module's unmatched spawn hook.
  on('agent.spawn', { background: [true, false] }, async ($, e, next) => {
    const result = await next(e);
    if (paneOpen && !e.resume && result.agentId && !result.deny) {
      // Refresh routing logs async to not delay subagent startup.
      void refreshOpenPane($, options).catch(() => {});
    }
    return result;
  });

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    if (e.args.trim() !== 'logs') return next(e);
    if (!launchedEnabled) return { text: 'Smart Router was not enabled for this session.' };
    detailRequest++;
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
          onPress: () => { detailRequest++; detail = null; $.ui.invalidate('ui.render'); },
        }),
      ];
      if (detail.failed) {
        children.push(Text({ children: ['Could not load this routing decision.'] }));
        if (detail.hint) children.push(Text({ dimColor: true, children: [detail.hint.slice(0, columns)] }));
      } else {
        // One labeled line per routing outcome; every label is dim so the block
        // reads consistently, and each value wraps in its own column. The label
        // column is fixed at the longest label plus a one-space gutter, so no
        // label ever wraps or shrinks and every value starts at the same column.
        const labelWidth = 'Prompt preview: '.length + 1;
        const labeledRow = (label: string, value: ReturnType<typeof Text>) =>
          Box({
            flexDirection: 'row',
            children: [
              Box({ width: labelWidth, flexShrink: 0, children: [Text({ dimColor: true, children: [label] })] }),
              Box({ flexGrow: 1, children: [value] }),
            ],
          });
        children.push(labeledRow('Prompt preview: ', Text({ children: [detail.preview ?? '(prompt not recorded)'] })));
        if (detail.subagent) children.push(labeledRow('Agent type: ', Text({ children: [detail.subagent] })));
        children.push(labeledRow('Selected: ', Text({ color: 'green', bold: true, children: [shortModel(detail.selected)] })));
        children.push(labeledRow('Routing Time: ', Text({ children: [`${(detail.routing_ms / 1000).toFixed(1)}s`] })));
        children.push(labeledRow('Rationale: ', Text({ children: [detail.rationale?.trim() || 'No rationale returned.'] })));
        children.push(Text({ dimColor: true, children: ['Candidates'] }));
        for (const candidate of detail.candidates) {
          children.push(Text({ children: [`${candidate === detail.selected ? '● ' : '○ '}${shortModel(candidate)}`] }));
        }
        const detailId = detail.id;
        children.push(...explanationSection($, e, detail, (id) => {
          if (!detail || detail.id !== detailId) return;
          detail = { ...detail, explanationModel: id };
          $.ui.invalidate('ui.render');
        }, () => {
          if (!detail || detail.id !== detailId) return;
          detail = { ...detail, showNumbers: !detail.showNumbers };
          $.ui.invalidate('ui.render');
        }));
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
        // The label prop is the only portable form: 2.1.290 rejects mixed
        // string/Text children ("takes one string child, its label, or none").
        label: `${age(row.at).padStart(8)}  ${label.padEnd(room)}  ${model}`,
        onPress: async () => {
          const request = ++detailRequest;
          const loaded = await openDecision($, options, row.decision_id);
          if (request !== detailRequest) return;
          detail = loaded;
          $.ui.invalidate('ui.render');
        },
      }));
    }
    return Box({ flexDirection: 'column', children });
  });
}
