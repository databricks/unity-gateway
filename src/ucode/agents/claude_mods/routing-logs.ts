import type { EngineInterface, On, PluginOptions } from 'claude-code';

export const ROUTING_LOGS_PANE = 'smart-router-logs';
export const LOGS_COMMAND_TIMEOUT_MS = 10000;
export const STARTUP_CHECK_ROW = "Smart routing disabled: This session's model list isn't supported by Smart Router";
const STARTUP_CHECK_REASONS: Record<string, string> = {
  insufficient_eligible_options: 'At least two supported models are required',
  missing_required_tier_option: 'Required models are missing from this list',
  INCOMPLETE_REQUIRED_OPTIONS: 'Required models are missing from this list',
  harness_not_supported: 'Harness not supported',
  mixed_harnesses_not_supported: 'Mixed harnesses not supported',
};
const STARTUP_CHECK_GENERIC_REASON = "Smart Routing does not support this agent's model list.";
const MODEL_REASONS: Record<string, string> = {
  model_not_supported: 'Model source not supported',
  model_not_in_recipe: 'Not available in Smart Router',
  excluded_by_policy: 'Excluded by routing policy',
  model_service_unresolved: 'Model service unresolved',
  model_provider_service_unresolved: 'Model provider service unresolved',
  harness_not_supported: '',
};
const MODEL_GENERIC_REASON = 'Not eligible for routing';
// Reason codes are identifiers; anything else is a server-written sentence shown as sent.
const REASON_CODE = /^[A-Za-z0-9_]+$/;
type StartupCheck = {
  check_id: string;
  at: number;
  reason: string | null;
  eligible: string[];
  ineligible: { model: string; reason: string | null }[];
  available: string[];
  validation_ms: number;
};
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
};

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '');

const isDecision = (d: Decision): boolean =>
  Boolean(d) && typeof d.decision_id === 'string' && Boolean(d.decision_id) &&
  Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
  (d.preview === null || typeof d.preview === 'string') && typeof d.selected_model === 'string' &&
  Number.isFinite(d.routing_ms);

const isStringList = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(item => typeof item === 'string');

const isStartupCheck = (c: StartupCheck): boolean =>
  Boolean(c) && typeof c.check_id === 'string' && Boolean(c.check_id) &&
  Number.isFinite(c.at) && Number.isFinite(new Date(c.at * 1000).getTime()) &&
  (c.reason === null || typeof c.reason === 'string') && isStringList(c.eligible) &&
  isStringList(c.available) && Number.isFinite(c.validation_ms) && Array.isArray(c.ineligible) &&
  c.ineligible.every(option => option !== null && typeof option === 'object' &&
    typeof option.model === 'string' && (option.reason === null || typeof option.reason === 'string'));

export function startupCheckReason(reason: string | null): string {
  if (reason !== null && Object.hasOwn(STARTUP_CHECK_REASONS, reason)) return STARTUP_CHECK_REASONS[reason];
  return reason?.trim() && !REASON_CODE.test(reason.trim()) ? reason.trim() : STARTUP_CHECK_GENERIC_REASON;
}

export function modelReason(reason: string | null): string {
  return reason !== null && Object.hasOwn(MODEL_REASONS, reason) ? MODEL_REASONS[reason] : MODEL_GENERIC_REASON;
}

// A v1 decision record: ignores unknown keys (a later `explanation`, for one).
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
    candidates: candidates.map(candidate => (candidate as { model: string }).model),
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
      { timeoutMs: LOGS_COMMAND_TIMEOUT_MS },
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
let checks: StartupCheck[] = [];
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
    const result = await $.process.run([...command, '--session', session], { timeoutMs: LOGS_COMMAND_TIMEOUT_MS });
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
    checks = [];
    failed = true;
    return `Smart Router logs: ${readError}`;
  }
  try {
    let data;
    try { data = JSON.parse(stdout); } catch { throw new Error('invalid logs JSON'); }
    if (data?.v !== 1 || !Array.isArray(data.decisions)) throw new Error('invalid logs response');
    rows = data.decisions.filter(isDecision).reverse();
    checks = Array.isArray(data.startup_checks) ? data.startup_checks.filter(isStartupCheck).reverse() : [];
    failed = false;
    return '';
  } catch (error) {
    rows = [];
    checks = [];
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

export function registerRoutingLogs(on: On, options: PluginOptions, launchedEnabled: boolean): void {
  // The open decision and its request token: a newer open or a logs rerun retires a load.
  let detail: Detail | null = null;
  let checkDetail: StartupCheck | null = null;
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
    checkDetail = null;
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
    // Each value wraps in its own column after a fixed, dim label column.
    const labeledRow = (labelWidth: number, label: string, value: ReturnType<typeof Text>) =>
      Box({
        flexDirection: 'row',
        children: [
          Box({ width: labelWidth, flexShrink: 0, children: [Text({ dimColor: true, children: [label] })] }),
          Box({ flexGrow: 1, children: [value] }),
        ],
      });
    const back = () => Button({
      key: 'back', plain: true, autoFocus: true, children: ['← Back'],
      onPress: () => { detailRequest++; detail = null; checkDetail = null; $.ui.invalidate('ui.render'); },
    });
    if (checkDetail) {
      const labelWidth = 'Validation Time: '.length + 1;
      const children = [
        back(),
        labeledRow(labelWidth, 'Reason: ', Text({ children: [startupCheckReason(checkDetail.reason)] })),
        labeledRow(labelWidth, 'Validation Time: ', Text({ children: [`${(checkDetail.validation_ms / 1000).toFixed(1)}s`] })),
      ];
      if (checkDetail.eligible.length || checkDetail.ineligible.length) {
        children.push(Text({ dimColor: true, children: ['Models'] }));
        for (const model of checkDetail.eligible) {
          children.push(Text({ children: [`✓ ${shortModel(model)}`] }));
        }
        for (const option of checkDetail.ineligible) {
          const reason = modelReason(option.reason);
          children.push(Text({ children: [`✗ ${shortModel(option.model)}${reason ? `  ${reason}` : ''}`] }));
        }
      }
      if (checkDetail.available.length) {
        children.push(Text({ dimColor: true, children: ['Supported models'] }));
        for (const model of checkDetail.available) children.push(Text({ children: [`  ${shortModel(model)}`] }));
      }
      return Box({ flexDirection: 'column', children });
    }
    if (detail) {
      const children = [back()];
      if (detail.failed) {
        children.push(Text({ children: ['Could not load this routing decision.'] }));
        if (detail.hint) children.push(Text({ dimColor: true, children: [detail.hint.slice(0, columns)] }));
      } else {
        // The label column is the longest label plus a one-space gutter, so no label wraps.
        const labelWidth = 'Prompt preview: '.length + 1;
        children.push(labeledRow(labelWidth, 'Prompt preview: ', Text({ children: [detail.preview ?? '(prompt not recorded)'] })));
        if (detail.subagent) children.push(labeledRow(labelWidth, 'Agent type: ', Text({ children: [detail.subagent] })));
        children.push(labeledRow(labelWidth, 'Selected: ', Text({ color: 'green', bold: true, children: [shortModel(detail.selected)] })));
        children.push(labeledRow(labelWidth, 'Routing Time: ', Text({ children: [`${(detail.routing_ms / 1000).toFixed(1)}s`] })));
        children.push(labeledRow(labelWidth, 'Rationale: ', Text({ children: [detail.rationale?.trim() || 'No rationale returned.'] })));
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
    const entries: (Decision | StartupCheck)[] = [...rows, ...checks].sort((a, b) => b.at - a.at);
    for (const row of entries) {
      if ('check_id' in row) {
        // Age (8) + "  " + the fixed text fills the row.
        const room = Math.max(1, columns - 10);
        const text = STARTUP_CHECK_ROW.length > room ? STARTUP_CHECK_ROW.slice(0, Math.max(0, room - 1)) + '…' : STARTUP_CHECK_ROW;
        children.push(Button({
          key: row.check_id, plain: true, label: `${age(row.at).padStart(8)}  ${text}`,
          onPress: () => { detailRequest++; checkDetail = row; $.ui.invalidate('ui.render'); },
        }));
        continue;
      }
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
