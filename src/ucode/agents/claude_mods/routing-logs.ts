import type { EngineInterface, On, PluginOptions } from 'claude-code';
import { shortModel } from './subagent-routing.ts';

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
  promptLength: number | null;
  promptHash: string | null;
  subagent: string | null;
  selected: string;
  requested: string | null;
  routing_ms: number;
  rationale: string | null;
  candidates: string[];
  explanation: Explanation | null;
  explanationInvalid: boolean;
};
type ExplanationModel = {
  id: string; name: string; reason: string; selected: boolean; considered: boolean;
  quality_estimate?: number; routing_score?: number; estimated_cost_usd?: number;
  components?: Record<string, number>; difference_from_winner?: Record<string, number>;
};
// The gateway's raw snake_case routing explanation, carried on the decision record.
type Explanation = {
  selected_model: string; summary: string; task_understanding: string[]; models: ExplanationModel[];
  recipe?: string; formula?: string;
};

const MAX_EXPLANATION_BYTES = 64 * 1024;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const isObject = (value: unknown): value is Record<string, any> =>
  Boolean(value && typeof value === 'object' && !Array.isArray(value));

// The canonical model id: no gateway wrapper, no `system.ai.` prefix, no context suffix.
const canonicalModel = (model: string): string =>
  model.replace(/^anthropic-aigw-[0-9a-fA-F]{8}-/, '').replace(/^system\.ai\./, '').replace(/\[1m\]$/, '');

const safeText = (value: unknown, limit: number): string | undefined => {
  if (typeof value !== 'string') return undefined;
  const clean = value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  // Over-limit text is cut, as #1513 does, so one long field never hides the explanation.
  return clean ? clean.slice(0, limit) : undefined;
};

const safeNumber = (value: unknown, nonNegative = false): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e12 && (!nonNegative || value >= 0)
    ? value : undefined;

// Parses an optional candidate field; a present-but-invalid value rejects the explanation.
const optional = <T,>(value: unknown, parse: (input: unknown) => T | undefined): T | undefined => {
  if (value === undefined) return undefined;
  const parsed = parse(value);
  if (parsed === undefined) throw new Error('invalid candidate numbers');
  return parsed;
};

function numericDetails(value: unknown): Record<string, number> | undefined {
  if (!isObject(value) || Object.keys(value).length > 8) return undefined;
  const entries = Object.entries(value);
  if (entries.some(([key, number]) => !/^[A-Za-z][A-Za-z0-9_. -]{0,63}$/.test(key)
    || safeNumber(number) === undefined)) return undefined;
  return Object.fromEntries(entries as [string, number][]);
}

const utf8Length = (value: string): number => new TextEncoder().encode(value).length;

// Throws on anything that does not match the explanation shape; the caller treats the
// explanation as absent, so a bad payload can never break the rest of the detail view.
function parseExplanation(value: unknown, routedModel: string): Explanation {
  if (!isObject(value)) throw new Error('explanation is not an object');
  if (Object.keys(value).length > 32 || utf8Length(JSON.stringify(value)) > MAX_EXPLANATION_BYTES) {
    throw new Error('explanation is too large');
  }
  const selectedModel = safeText(value.selected_model, 160);
  const summary = safeText(value.summary, 2000);
  if (!selectedModel || !MODEL_ID.test(selectedModel) || selectedModel !== canonicalModel(routedModel) || !summary
    || !Array.isArray(value.models) || value.models.length < 1
    || !Array.isArray(value.task_understanding) || value.task_understanding.length > 8) {
    throw new Error('invalid explanation header');
  }
  const taskUnderstanding = value.task_understanding.map((item: unknown) => safeText(item, 240));
  if (taskUnderstanding.some((item: string | undefined) => !item)) throw new Error('invalid task understanding');
  const ids = new Set<string>();
  const models: ExplanationModel[] = [];
  for (const candidate of value.models) {
    if (!isObject(candidate)) throw new Error('invalid candidate');
    const id = safeText(candidate.id, 160);
    const name = safeText(candidate.name, 128);
    const reason = safeText(candidate.reason, 1200);
    if (!id || !MODEL_ID.test(id) || ids.has(id) || !name || !reason
      || typeof candidate.selected !== 'boolean' || typeof candidate.considered !== 'boolean'
      || (candidate.selected && !candidate.considered)) throw new Error('invalid candidate');
    ids.add(id);
    models.push({
      id, name, reason, selected: candidate.selected, considered: candidate.considered,
      quality_estimate: optional(candidate.quality_estimate, safeNumber),
      routing_score: optional(candidate.routing_score, safeNumber),
      estimated_cost_usd: optional(candidate.estimated_cost_usd, (input) => safeNumber(input, true)),
      components: optional(candidate.components, numericDetails),
      difference_from_winner: optional(candidate.difference_from_winner, numericDetails),
    });
  }
  if (models.filter((model) => model.selected).length !== 1
    || !models.some((model) => model.id === selectedModel && model.selected)) {
    throw new Error('explanation selection mismatch');
  }
  const recipe = value.recipe === undefined ? undefined : safeText(value.recipe, 240);
  const formula = value.formula === undefined ? undefined : safeText(value.formula, 500);
  if ((value.recipe !== undefined && !recipe) || (value.formula !== undefined && !formula)) {
    throw new Error('invalid recipe or formula');
  }
  return { selected_model: selectedModel, summary, task_understanding: taskUnderstanding as string[], models, recipe, formula };
}

const hasNumbers = (model: ExplanationModel): boolean =>
  model.quality_estimate !== undefined || model.routing_score !== undefined || model.estimated_cost_usd !== undefined;

const numbersLine = (model: ExplanationModel): string | null => {
  const parts: string[] = [];
  if (model.quality_estimate !== undefined) parts.push(`quality ${model.quality_estimate.toFixed(3)}`);
  if (model.routing_score !== undefined) parts.push(`score ${model.routing_score.toFixed(3)}`);
  if (model.estimated_cost_usd !== undefined) parts.push(`cost $${model.estimated_cost_usd.toFixed(3)}`);
  return parts.length ? parts.join(' · ') : null;
};

// A v1 decision record: ignores unknown keys except the optional explanation, which is
// validated separately so an invalid one degrades to the basic view instead of failing.
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
  let explanation: Explanation | null = null;
  let explanationInvalid = false;
  if (d.explanation != null) {
    try {
      explanation = parseExplanation(d.explanation, d.selected_model as string);
    } catch {
      explanationInvalid = true;
    }
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
    explanation,
    explanationInvalid,
  };
}

function failedDetail(id: string): Detail {
  return {
    id, failed: true, preview: null, promptLength: null, promptHash: null,
    subagent: null, selected: '', requested: null, routing_ms: 0, rationale: null,
    candidates: [], explanation: null, explanationInvalid: false,
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
    const detail = parseDecision(result.stdout, id);
    if (detail.explanationInvalid) {
      $.ui.log('Smart Router logs: invalid routing explanation; showing the basic view', { to: 'debug' });
    }
    return detail;
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

const isDecision = (d: Decision): boolean =>
  Boolean(d) && typeof d.decision_id === 'string' && Boolean(d.decision_id) &&
  Number.isFinite(d.at) && Number.isFinite(new Date(d.at * 1000).getTime()) &&
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
export function logsReaderCommand(options: PluginOptions): string[] {
  const command = options.logsCommand as string[];
  if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string' || !arg)) {
    throw new Error('logs command unavailable');
  }
  return command;
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
    let note: string | undefined;
    try {
      const command = logsReaderCommand(options);
      const session = await $.session.id();
      const result = await $.process.run([...command, '--session', session], { timeoutMs: 10000 });
      note = applyRoutingLogsRead(result.stdout,
        result.exitCode !== 0 || result.isStdoutTruncated
          ? `logs command exited ${result.exitCode} or truncated output`
          : undefined);
    } catch (error) {
      note = applyRoutingLogsRead('', error instanceof Error ? error.message : 'read failed');
    }
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
  // The open decision and its request token: a newer open or a logs rerun retires a load.
  let detail: Detail | null = null;
  let detailRequest = 0;
  let showNumbers = false;

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
        if (detail.explanation) {
          const explanation = detail.explanation;
          children.push(Text({ dimColor: true, children: ['Summary'] }));
          children.push(Text({ children: [explanation.summary] }));
          for (const item of explanation.task_understanding) {
            children.push(Text({ dimColor: true, children: [`• ${item}`] }));
          }
          children.push(Text({ dimColor: true, children: ['Candidates'] }));
          for (const model of explanation.models) {
            const label = `${model.selected ? '● ' : '○ '}${model.name}${model.considered ? '' : ' (not considered)'}`;
            children.push(model.selected
              ? Text({ color: 'green', bold: true, children: [label] })
              : Text({ children: [label] }));
            children.push(Text({ dimColor: true, children: [`  ${model.reason}`] }));
            if (showNumbers) {
              const numbers = numbersLine(model);
              if (numbers !== null) children.push(Text({ dimColor: true, children: [`    ${numbers}`] }));
            }
          }
          if (explanation.models.some(hasNumbers)) {
            children.push(Button({
              key: 'numbers', plain: true, children: [showNumbers ? 'Hide numbers' : 'Show numbers'],
              onPress: () => { showNumbers = !showNumbers; $.ui.invalidate('ui.render'); },
            }));
          }
        } else {
          children.push(Text({ dimColor: true, children: ['Candidates'] }));
          for (const candidate of detail.candidates) {
            children.push(Text({ children: [`${candidate === detail.selected ? '● ' : '○ '}${shortModel(candidate)}`] }));
          }
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
        // The label prop is the only portable form: 2.1.290 rejects mixed
        // string/Text children ("takes one string child, its label, or none").
        label: `${age(row.at).padStart(8)}  ${label.padEnd(room)}  ${model}`,
        onPress: async () => {
          showNumbers = false;
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
