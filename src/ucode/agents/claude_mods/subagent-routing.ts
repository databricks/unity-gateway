import type { On, PluginOptions } from 'claude-code'
import { matchesAutoModel } from './auto-model.ts'
import { isRoutingEnabled } from './routing-state.ts'

type NumericDetails = Record<string, number>
export type ExplanationModel = {
  id: string; name: string; selected: boolean; considered: boolean; reason: string
  qualityEstimate?: number; estimatedCostUsd?: number; routingScore?: number
  components?: NumericDetails; differenceFromWinner?: NumericDetails
}
export type RoutingExplanation = {
  selectedModel: string; summary: string; taskUnderstanding: string[]; models: ExplanationModel[]
  formula?: string
}
type RoutingRecord = {
  name: string; title: string; model: string; rationale: string; seconds?: number
  spawned?: { agentId: string; model?: string; background: boolean }
}

const MAX_EXPLANATION_BYTES = 64 * 1024
const isObject = (value: unknown): value is Record<string, any> =>
  Boolean(value && typeof value === 'object' && !Array.isArray(value))
const safeText = (value: unknown, limit: number): string | undefined => {
  if (typeof value !== 'string') return undefined
  const clean = value
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, limit)
  return clean || undefined
}

const safeNumber = (value: unknown, nonNegative = false): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= 1e12 && (!nonNegative || value >= 0)
    ? value : undefined

function numericDetails(value: unknown): NumericDetails | undefined {
  if (!isObject(value) || Object.keys(value).length > 8) return undefined
  const entries = Object.entries(value)
  if (entries.some(([key, number]) => !/^[A-Za-z][A-Za-z0-9_. -]{0,63}$/.test(key)
      || safeNumber(number) === undefined)) return undefined
  return Object.fromEntries(entries as [string, number][])
}

const unwrapGatewayModel = (model: string): string =>
  model.replace(/^anthropic-aigw-[0-9a-fA-F]{7,8}-(?=.)/, '')

const explanationModelId = (model: string): string => {
  const original = unwrapGatewayModel(model)
  if (original.startsWith('system.ai.')) return original.slice('system.ai.'.length).replace(/\[1m\]$/, '')
  // Explanation IDs preserve every catalog suffix; execution matching below also accepts
  // native [1m] expansion on dotted model versions such as main.default.claude-sonnet-4.5.
  return original.split('.').length >= 3 ? original : original.replace(/\[1m\]$/, '')
}

const canonicalModel = (model: string): string => {
  const original = model.replace(/^anthropic-aigw-[0-9a-fA-F]{8}-/, '')
  if (original.startsWith('system.ai.')) return original.slice('system.ai.'.length).replace(/\[1m\]$/, '')
  return original.split('.').length === 3 ? original : original.replace(/\[1m\]$/, '')
}

function matchesIdentityFields(value: Record<string, any>, sessionId?: string, toolUseId?: string): boolean {
  for (const [field, expected] of [['session_id', sessionId], ['tool_use_id', toolUseId]] as const) {
    const actual = value[field]
    if (actual != null && (typeof actual !== 'string' || actual.length > 256 || actual !== expected)) return false
  }
  return true
}

function matchesRequestIdentity(value: unknown, sessionId?: string, toolUseId?: string): boolean {
  if (!isObject(value)) return true
  return [value, value.evidence].every((source) => !isObject(source)
    || matchesIdentityFields(source, sessionId, toolUseId))
}

export function parseRoutingExplanation(
  value: unknown, routedModel: string, sessionId?: string, toolUseId?: string, offeredModels?: readonly string[],
): RoutingExplanation | undefined {
  if (!isObject(value)) return undefined
  const serialized = JSON.stringify(value)
  if (Object.keys(value).length > 32 || new TextEncoder().encode(serialized).byteLength > MAX_EXPLANATION_BYTES
      || !matchesRequestIdentity(value, sessionId, toolUseId)) return undefined
  const selectedModel = value.selected_model
  const summary = safeText(value.summary, 2000)
  if (typeof selectedModel !== 'string' || selectedModel.trim() !== selectedModel
      || !/^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,159}$/.test(selectedModel)
      || selectedModel !== explanationModelId(routedModel) || !summary
      || !Array.isArray(value.models) || value.models.length < 1
      || !Array.isArray(value.task_understanding) || value.task_understanding.length > 8) return undefined

  const taskUnderstanding = value.task_understanding.map((item: unknown) => safeText(item, 240))
  if (taskUnderstanding.some((item: string | undefined) => !item)) return undefined
  const ids = new Set<string>()
  const models: ExplanationModel[] = []
  for (const candidate of value.models) {
    if (!isObject(candidate)) return undefined
    const id = candidate.id
    const name = safeText(candidate.name, 128)
    const reason = safeText(candidate.reason, 1200)
    if (typeof id !== 'string' || id.trim() !== id
        || !/^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,159}$/.test(id) || ids.has(id)
        || !name || !reason || typeof candidate.selected !== 'boolean'
        || typeof candidate.considered !== 'boolean' || (candidate.selected && !candidate.considered)) return undefined
    ids.add(id)
    const qualityEstimate = candidate.quality_estimate === undefined ? undefined : safeNumber(candidate.quality_estimate)
    const estimatedCostUsd = candidate.estimated_cost_usd === undefined ? undefined : safeNumber(candidate.estimated_cost_usd, true)
    const routingScore = candidate.routing_score === undefined ? undefined : safeNumber(candidate.routing_score)
    const components = candidate.components === undefined ? undefined : numericDetails(candidate.components)
    const differenceFromWinner = candidate.difference_from_winner === undefined
      ? undefined : numericDetails(candidate.difference_from_winner)
    if ((candidate.quality_estimate !== undefined && qualityEstimate === undefined)
        || (candidate.estimated_cost_usd !== undefined && estimatedCostUsd === undefined)
        || (candidate.routing_score !== undefined && routingScore === undefined)
        || (candidate.components !== undefined && components === undefined)
        || (candidate.difference_from_winner !== undefined && differenceFromWinner === undefined)) return undefined
    models.push({ id, name, reason, selected: candidate.selected, considered: candidate.considered,
      qualityEstimate, estimatedCostUsd, routingScore, components, differenceFromWinner })
  }
  if (models.filter((model) => model.selected).length !== 1
      || !models.some((model) => model.id === selectedModel && model.selected)) return undefined
  if (offeredModels) {
    const offeredIds = new Set(offeredModels.map(explanationModelId))
    if (ids.size !== offeredIds.size || [...offeredIds].some((id) => !ids.has(id))) return undefined
  }
  const formula = value.formula === undefined ? undefined : safeText(value.formula, 500)
  if (value.formula !== undefined && !formula) return undefined
  return { selectedModel, summary, taskUnderstanding: taskUnderstanding as string[], models, formula }
}

const children = new Map<string, RoutingRecord>()
export const routedModels = {
  started(record: RoutingRecord) { children.set(record.spawned!.agentId, record) },
  record(agentId?: string) { return agentId ? children.get(agentId) : undefined },
  model(agentId?: string) {
    const child = agentId ? children.get(agentId) : undefined
    const resolved = child?.spawned?.model
    return child && resolved && canonicalModel(resolved) === canonicalModel(child.model)
      ? resolved : child?.model
  },
  reset() { children.clear() },
}

const ANIMALS = [
  'Otter', 'Panda', 'Alpaca', 'Fox', 'Badger', 'Beaver', 'Bison', 'Bobcat', 'Camel', 'Caribou',
  'Cheetah', 'Chipmunk', 'Cougar', 'Coyote', 'Deer', 'Dingo', 'Dolphin', 'Donkey', 'Elephant', 'Elk',
  'Ferret', 'Gazelle', 'Giraffe', 'Goat', 'Gopher', 'Gorilla', 'Hamster', 'Hare', 'Hedgehog', 'Hippo',
  'Horse', 'Hyena', 'Ibex', 'Impala', 'Jaguar', 'Kangaroo', 'Koala', 'Lemur', 'Leopard', 'Lion',
  'Lynx', 'Marmot', 'Meerkat', 'Mink', 'Mole', 'Mongoose', 'Moose', 'Mouse', 'Narwhal', 'Ocelot',
  'Okapi', 'Orca', 'Pangolin', 'Platypus', 'Porcupine', 'Possum', 'Puma', 'Quokka', 'Rabbit', 'Raccoon',
  'Reindeer', 'Rhino', 'Seal', 'Sloth', 'Squirrel', 'Tapir', 'Tiger', 'Vole', 'Walrus', 'Warthog',
  'Weasel', 'Whale', 'Wolf', 'Wombat', 'Yak', 'Zebra', 'Albatross', 'Crane', 'Crow', 'Dove',
  'Eagle', 'Falcon', 'Finch', 'Flamingo', 'Heron', 'Ibis', 'Jay', 'Kestrel', 'Kiwi', 'Lark',
  'Magpie', 'Osprey', 'Owl', 'Parrot', 'Pelican', 'Penguin', 'Puffin', 'Raven', 'Robin', 'Swan',
]

const shortModel = (model: string): string => {
  const unwrapped = unwrapGatewayModel(model)
  const parts = unwrapped.split('.')
  const name = parts.length > 3 ? parts.slice(3).join('.')
    : parts.length === 3 ? parts[2] : unwrapped
  return name.replace(/^databricks-/, '').replace(/\[1m\]$/, '')
}
// The selected model is only a prediction until the spawn receipt names the launched one.
const differs = (selected: string, launched: string): boolean =>
  canonicalModel(launched) !== canonicalModel(selected)
// The launch line of a card: the launched model, flagged when it is not the selected one.
const launchNote = (selected: string, launched?: string): string => {
  if (!launched) return 'Launch model not reported; labels cannot be verified'
  return differs(selected, launched)
    ? `⚠ Launched Model: ${shortModel(launched)} (differs from the selected model)`
    : `Launched Model: ${shortModel(launched)}`
}
// Flags a launch that is not the selected model.
const mismatchNote = (selected: string, launched: string): string =>
  differs(selected, launched) ? ` ⚠ selected ${shortModel(selected)}` : ''
const taskLabel = (record: RoutingRecord): string => {
  const launched = record.spawned?.model
  if (!launched) return `${record.name} — ${shortModel(record.model)} (selected, launch pending)`
  return `${record.name} — ${shortModel(launched)}${mismatchNote(record.model, launched)}`
}
// The selection label is only a prediction until the spawn receipt arrives.
const heading = (record: RoutingRecord): string =>
  `${record.name} · ${shortModel(record.model)} · ${record.title}`
// A neutral task title: the prompt's first sentence, so a description a model wrote around the
// model it was asked to use is only a fallback when the prompt is empty.
const taskTitle = (input: Record<string, any>): string =>
  safeText(input.prompt, 400)?.split(/(?<=[.!?])\s/)[0].replace(/[.!?]+$/, '').slice(0, 60).trim()
  || safeText(input.description, 60) || 'Subagent task'
type RoutedDisplay = {
  session_id: string; agent_id: string; name: string; description: string
  animal: string; title: string; selected: string; launched: string
}
// The receipt-backed label: the launched model, flagged when the selection differs.
const receiptLabel = (d: Pick<RoutedDisplay, 'animal' | 'title' | 'selected' | 'launched'>): string =>
  `${d.animal} · ${shortModel(d.launched)} · ${d.title}${mismatchNote(d.selected, d.launched)}`
const quotedDescriptionEnd = (text: string, description: string): number => {
  const prefix = 'Agent "'
  if (!text.startsWith(prefix)) return -1
  let closingQuote = -1
  let quote = text.indexOf('"', prefix.length)
  while (quote >= 0) {
    const candidate = safeText(text.slice(prefix.length, quote), 1000)
    const after = text[quote + 1]
    // A mismatched prefix cannot become the recorded description at a later quote.
    if (candidate && !description.startsWith(candidate)) break
    // Embedded quotes may match a prefix; prefer the full heading when it is present.
    if (candidate && (!after || /[^\p{L}\p{N}_]/u.test(after))) {
      closingQuote = quote
      if (candidate === description) break
    }
    quote = text.indexOf('"', quote + 1)
  }
  return closingQuote
}
export const routedDisplays: {
  sessionId?: string; root?: string; records: Map<string, RoutedDisplay>
} = { records: new Map() }
export const savedRoutedDisplay = (value: unknown, sessionId: string): RoutedDisplay | undefined => {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !('session_id' in value) || value.session_id !== sessionId
      || !('agent_id' in value) || typeof value.agent_id !== 'string' || !value.agent_id
      || !('name' in value) || !('description' in value)) return undefined
  const [name, description, animal, title, selected, launched] = ['name', 'description', 'animal', 'title', 'selected', 'launched']
    .map((key) => safeText((value as Record<string, unknown>)[key], key === 'description' ? 1000 : 240))
  return name && description && animal && title && selected && launched
    ? { session_id: sessionId, agent_id: value.agent_id, name, description, animal, title, selected, launched } : undefined
}
// Claude's own spinner glyphs, played forwards then back as Claude does.
const SPINNER_GLYPHS = ['·', '✢', '✳', '✶', '✻', '✽']
const SPINNER = [...SPINNER_GLYPHS, ...SPINNER_GLYPHS.slice(1, -1).reverse()]
const SPINNER_INTERVAL_MS = 100

// The timer is cosmetic: without a clock it is omitted rather than blocking routing.
async function now($: any): Promise<number | undefined> {
  try {
    return await $.clock.now()
  } catch {
    return undefined
  }
}
async function secondsSince($: any, start: number | undefined): Promise<number | undefined> {
  const current = start === undefined ? undefined : await now($)
  return current === undefined || start === undefined ? undefined : Math.max(0, current - start) / 1000
}

// The selected model with its routing time, then the reason. Labels recede so the routed
// model, in the status band's ON green, stands out; the time is omitted without a clock.
const selection = ($: any, e: any, record: RoutingRecord, arrow: string, reasonPadding: number) => {
  const { Box, Text } = $.ui.resolve(e)
  const lines = [Box({
    flexDirection: 'row',
    children: [
      Text({ dimColor: true, children: [`${arrow}Selected Model: `] }),
      Text({ color: 'green', bold: true, children: [shortModel(record.model)] }),
      ...(record.seconds === undefined ? [] : [
        Text({ dimColor: true, children: [` (routed in ${record.seconds.toFixed(1)}s)`] }),
      ]),
    ],
  })]
  if (record.spawned) {
    const launched = record.spawned.model
    lines.push(Box({ paddingLeft: reasonPadding, children: [Text({
      color: launched && !differs(record.model, launched) ? undefined : 'red',
      children: [launchNote(record.model, launched)],
    })] }))
  }
  if (record.rationale) {
    lines.push(Box({
      paddingLeft: reasonPadding,
      children: [Text({ children: [Text({ dimColor: true, children: ['Reason: '] }), record.rationale] })],
    }))
  }
  return lines
}

// Use the neutral title; the written description may name an overridden model.
const card = ($: any, e: any, record: RoutingRecord) => {
  const { Box, Text } = $.ui.resolve(e)
  return Box({
    flexDirection: 'column',
    paddingLeft: 2,
    marginBottom: 1,
    children: [
      Text({ bold: true, children: [taskLabel(record)] }),
      Text({ dimColor: true, children: ['Task request: ', record.title] }),
      ...selection($, e, record, '↳ ', 2),
    ],
  })
}

const routing: { options?: PluginOptions; autoModels?: readonly string[] } = {}
const names = new Map<string, string>()
const unusedNames = [...ANIMALS]
// Calls waiting on the router, by when routing started, shown with a spinner and timer.
const pending = new Map<string, number | undefined>()
// Claude folds parallel Agent rows into its own block, which no hook reaches, so a call
// that was routing alongside another is drawn as a card above the parent's next reply.
// A lone call keeps its decision under its own row.
const grouped = new Set<string>()
const records = new Map<string, RoutingRecord>()
// Calls the router has already been asked about, so a failed answer is not asked for again.
const attempted = new Set<string>()
// The reply each batch is drawn above.
const anchors = new Map<string, string[]>()
// Replies already drawn before a batch was routed are never its anchor.
const seen = new Set<string>()


// Grouped calls not yet drawn, in call order. They live outside registration so the
// register module's one turn.complete handler can drop them.
const cards = { unplaced: [] as string[] }

// An aborted turn draws no reply, so its calls must not reach a later turn. A turn that
// merely ends, as when the reply waits on a background agent, keeps them.
export function endSubagentRoutingTurn(aborted: boolean): void {
  if (aborted) cards.unplaced = []
}

// Route each call once without altering the input auto mode may have reviewed.
async function routeCall($: any, e: any): Promise<void> {
  const { options, autoModels } = routing
  const command = options!.routeCommand as readonly string[]
  if (e.tool_use_id && (records.has(e.tool_use_id) || attempted.has(e.tool_use_id))) return
  if (!command.length || e.subagent_type === 'fork' || e.resume) return
  const selected = autoModels ? await $.session.model() : ''
  if (autoModels && (
    !autoModels.some(model => matchesAutoModel(model, selected)) || !await $.routingEnabled()
  )) return
  const { tool, tool_use_id, agentId, ...input } = e
  if (tool_use_id) attempted.add(tool_use_id)
  let route
  let spinner: { cancel: () => void } | undefined
  if (tool_use_id) {
    if (!names.has(tool_use_id)) {
      if (!unusedNames.length) unusedNames.push(...ANIMALS)
      const animal = unusedNames.splice(Math.floor(Math.random() * unusedNames.length), 1)[0]
      const cycle = Math.floor(names.size / ANIMALS.length)
      names.set(tool_use_id, cycle ? `${animal} ${cycle + 1}` : animal)
    }
    // Parallel calls start together, so one still routing means this call is not alone.
    if (pending.size) {
      for (const id of pending.keys()) {
        if (!grouped.has(id)) {
          grouped.add(id)
          cards.unplaced.push(id)
        }
      }
      grouped.add(tool_use_id)
      cards.unplaced.push(tool_use_id)
    }
    pending.set(tool_use_id, undefined)
    pending.set(tool_use_id, await now($))
    // Mods have no global timers; $.clock redraws the spinner while the router answers.
    // The frame comes from the clock, so parallel calls do not speed it up.
    try {
      spinner = $.clock.every(SPINNER_INTERVAL_MS, () => $.ui.invalidate('ui.render'))
    } catch {}
    $.ui.invalidate('ui.render')
  }
  try {
    const session_id = await $.session.id()
    const result = await $.process.run(command, {
      stdin: JSON.stringify({ session_id, agent_id: agentId, tool_use_id, tool_name: tool, tool_input: input }),
      timeoutMs: 35000,
    })
    if (result.exitCode !== 0) throw new Error('Router command failed')
    // An empty response means routing is disabled or no model could be selected.
    route = result.stdout.trim() ? JSON.parse(result.stdout) : undefined
    if (route !== undefined && (
        typeof route?.model !== 'string' || !route.model ||
        typeof route.rationale !== 'string')) {
      throw new Error('Invalid routing response')
    }
  } catch {
    route = undefined
    $.ui.log('Smart Router could not route this subagent; using its original settings.')
  } finally {
    spinner?.cancel()
  }
  const seconds = tool_use_id ? await secondsSince($, pending.get(tool_use_id)) : undefined
  if (tool_use_id) {
    pending.delete(tool_use_id)
    if (route) {
      records.set(tool_use_id, {
        name: names.get(tool_use_id)!,
        title: taskTitle(input),
        model: route.model,
        rationale: route.rationale,
        seconds,
      })
    }
    $.ui.invalidate('ui.render')
  }
}

// The recorded call is what auto mode reviews and what the grouped heading draws, so only
// the label-only description is rewritten, before either sees it.
export async function* routeAgentStream($: any, step: any, source: AsyncGenerator<any, any>): AsyncGenerator<any, any> {
  // Rewriting the reply would void a server-side auto-mode verdict, so that review keeps its input.
  if (await $.serverReview()) return yield* source
  // With the local fallback check off, a rewrite would leave the call with no usable verdict
  // and auto mode blocks it, so the stream passes through untouched; tool.call routes instead.
  if (await $.fallbackOff()) return yield* source
  // Nothing to head when routing is off, so the reply streams untouched. An Auto launch keeps
  // the model's heading too: its session model is a picker alias, which cannot stand in as
  // the fallback reviewer once the server-side verdict is void.
  if (!(routing.options?.routeCommand as readonly string[] | undefined)?.length || routing.autoModels
      || routing.options?.ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI === '0') {
    return yield* source
  }
  const held: any[] = []
  const calls = new Map<number, { tool: string; id: string; json: string }>()
  const release = async function* (): AsyncGenerator<any> {
    const inputs = new Map<number, Record<string, any>>()
    for (const [index, call] of calls) {
      try { inputs.set(index, JSON.parse(call.json || '{}')) } catch { /* leave the call as the model wrote it */ }
    }
    // A call that cannot be routed goes on as the model wrote it; it must not cost the reply.
    await Promise.all([...calls].map(async ([index, call]) => {
      if (!inputs.has(index)) return
      try {
        await routeCall($, { ...inputs.get(index), tool: call.tool, tool_use_id: call.id, agentId: step.agentId })
      } catch {
        $.ui.log('Smart Router could not route this subagent; using its original settings.')
      }
    }))
    const written = new Set<number>()
    for (const chunk of held) {
      const call = chunk.kind === 'input' ? calls.get(chunk.index) : undefined
      if (!call) { yield chunk; continue }
      if (written.has(chunk.index)) continue
      written.add(chunk.index)
      const record = records.get(call.id)
      const input = inputs.get(chunk.index)
      yield record && input ? { ...chunk, json: JSON.stringify({ ...input, description: heading(record) }) }
        : { ...chunk, json: call.json }
    }
    held.length = 0
    calls.clear()
  }
  // A consumer that stops reading must also stop the reply stream beneath this one.
  try {
    while (true) {
      const next = await source.next()
      if (next.done) { yield* release(); return next.value }
      const chunk = next.value
      if (chunk.kind === 'tool' && (chunk.name === 'Agent' || chunk.name === 'Task')) calls.set(chunk.index, { tool: chunk.name, id: chunk.id, json: '' })
      if (!calls.size) { yield chunk; continue }
      if (chunk.kind === 'input' && calls.has(chunk.index)) calls.get(chunk.index)!.json += chunk.json
      held.push(chunk)
      if (chunk.kind === 'stop') yield* release()
    }
  } finally {
    await source.return?.(undefined)
  }
}

export const registerSubagentRouting = (
  on: On,
  options: PluginOptions,
  autoModels?: readonly string[],
): void => {
  routedModels.reset()
  const showModel = options.ENABLE_SUBAGENT_MODEL_IN_MAIN_TUI !== '0'
  routing.options = options
  routing.autoModels = autoModels
  for (const state of [names, pending, records, anchors]) state.clear()
  for (const state of [grouped, seen, attempted]) state.clear()
  unusedNames.splice(0, unusedNames.length, ...ANIMALS)

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    await routeCall({
      session: { id: () => $.session.id(), model: () => $.session.model() },
      routingEnabled: async () => isRoutingEnabled({
        ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
      }),
      process: { run: (command: readonly string[], init: { stdin: string; timeoutMs: number }) => $.process.run(command, init) },
      clock: { now: () => $.clock.now(), every: (ms: number, tick: () => void) => $.clock.every(ms, tick) },
      ui: { invalidate: (name: 'ui.render') => $.ui.invalidate(name), log: (text: string) => $.ui.log(text) },
    }, e)
    const { tool_use_id } = e
    // Auto mode reviewed this input; model identity belongs in the result, not its arguments.
    const result = await next(e)
    const record = tool_use_id ? records.get(tool_use_id) : undefined
    if (result.deny !== undefined || result.isError || !record?.spawned) return result
    const identity = JSON.stringify({
      agent_id: record.spawned.agentId,
      selected_model: record.model,
      launched_model: record.spawned.model ?? null,
    })
    return { ...result, context: [...(result.context ?? []),
      `Smart Router launch: ${identity}. Use the launched_model from this receipt when identifying `
      + 'this child. Report only that model in ordinary updates and answers; do not compare it with the requested model unless the user asks about the difference.',
    ] }
  })

  on('agent.spawn', async ($, e, next) => {
    const route = e.resume ? undefined : records.get(e.tool_use_id)
    // The task shows the spawn description, so the routed label is set here — after the
    // permission decision, never on the reviewed call. On the rewrite path the recorded
    // description is already this heading; this is what labels the task in every other mode.
    const label = route && showModel ? heading(route) : undefined
    const result = await next(route ? { ...e, model: route.model, ...(label ? { description: label } : {}) } : e)
    if (route && result.agentId && !result.deny) {
      route.spawned = { agentId: result.agentId, model: result.model || undefined, background: e.background }
      routedModels.started(route)
      $.ui.invalidate('ui.render')
    }
    if (showModel && route && result.agentId && !result.deny && !result.model) {
      $.ui.log('Claude did not report the launched model; routed labels cannot be verified for this subagent.')
    } else if (showModel && route && result.agentId && !result.deny) {
      try {
        const session_id = await $.session.id()
        const saved = savedRoutedDisplay({ session_id, agent_id: result.agentId, name: taskLabel(route),
          description: safeText(e.description, 1000) || heading(route),
          animal: route.name, title: route.title, selected: route.model, launched: result.model,
        }, session_id)
        if (!saved) throw new Error('Invalid subagent display metadata')
        routedDisplays.sessionId ??= session_id
        if (routedDisplays.sessionId === session_id) routedDisplays.records.set(result.agentId, saved)
        const root = routedDisplays.root ??= await $.env.get('UCODE_AGENT_LABELS_DIR')
        if (!root) throw new Error('Subagent display directory is unavailable')
        const filename = `agent-${encodeURIComponent(result.agentId)}.json`
        await $.fs.write(`${root}/${encodeURIComponent(session_id)}/${filename}`, JSON.stringify(saved))
        // The native bottom bar is outside ui.render; join its task ID to this call.
        await $.fs.write(`${$.plugin.root}/agent-names/${filename}`, JSON.stringify(saved))
      } catch {
        $.ui.log('Could not persist the subagent display label; native labels remain where unavailable.')
      }
    }
    return result
  })

  // The native notice quotes whichever description the task carries — the one the model
  // wrote, or the spawn-time routed label — which can name the model the parent asked for;
  // the receipt's label replaces it at render time, and a differing launch is flagged.
  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const task = e.props.task
    if (!showModel || e.props.origin?.kind !== 'task-notification' || !task?.id) return next(e)
    const saved = routedDisplays.records.get(task.id)
    if (!saved || typeof e.props.text !== 'string') return next(e)
    const spawnedLabel = `${saved.animal} · ${shortModel(saved.selected)} · ${saved.title}`
    const closingQuote = Math.max(quotedDescriptionEnd(e.props.text, saved.description),
      quotedDescriptionEnd(e.props.text, spawnedLabel))
    const text = closingQuote >= 0
      ? `Agent "${receiptLabel(saved)}"${e.props.text.slice(closingQuote + 1)}` : e.props.text
    const flagged = differs(saved.selected, saved.launched) && text === e.props.text
      ? `${text}\n⚠ Launched on ${shortModel(saved.launched)}, not the selected ${shortModel(saved.selected)}` : text
    return flagged === e.props.text ? next(e) : next({ ...e, props: { ...e.props, text: flagged } })
  })

  // The agent list reads the launch receipt rather than the prediction.
  on('ui.render', { component: 'AgentRow' }, async ($, e, next) => {
    if (!showModel) return next(e)
    const live = routedModels.record(e.requestId)
    const saved = routedDisplays.records.get(e.requestId)
    const display = live?.spawned?.model
      ? { animal: live.name, title: live.title, selected: live.model, launched: live.spawned.model } : saved
    return next(display ? { ...e, props: { ...e.props, label: receiptLabel(display) } } : e)
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const record = records.get(e.requestId)
    const name = names.get(e.requestId)
    const input = e.props.input
    const original = await next(name && (record || pending.has(e.requestId)) ? {
      ...e,
      props: { ...e.props, tool: name,
        ...(showModel && record && input !== null && typeof input === 'object' && !Array.isArray(input)
          ? { input: { ...input, description: record.spawned?.model
            ? receiptLabel({ animal: record.name, title: record.title, selected: record.model, launched: record.spawned.model })
            : heading(record) } } : {}),
      },
    } : e)
    // The in-progress line names no model, so blind configs still show it; only the
    // decided selection names the model and stays hidden when showModel is false.
    const nothingToShow = record ? !showModel : !pending.has(e.requestId)
    if (nothingToShow) return original

    const { Box, Text } = $.ui.resolve(e)
    if (!record) {
      const glyph = SPINNER[Math.floor(((await now($)) ?? 0) / SPINNER_INTERVAL_MS) % SPINNER.length]
      const seconds = await secondsSince($, pending.get(e.requestId))
      const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
      return Box({ flexDirection: 'column', children: [original, Box({
        flexDirection: 'row',
        children: [
          Text({ dimColor: true, children: ['  ↳ '] }),
          Text({ color: 'green', children: [`${glyph} `] }),
          Text({ dimColor: true, children: [`Selecting a model…${elapsed}`] }),
        ],
      })] })
    }
    if (grouped.has(e.requestId)) return original
    // The native lone row already includes the task title; another line would repeat it.
    return Box({ flexDirection: 'column', children: [original,
      ...selection($, e, record, '  ↳ ', 4),
    ] })
  })

  // Claude's parallel-agent block hides the row spinner, so the turn's own spinner line
  // also says routing is under way, timed from the oldest call still waiting. Its text
  // names no model, so blind configs show it too; only the decided selection is hidden.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    if (!pending.size) return next(e)
    const starts = [...pending.values()].filter((start) => start !== undefined) as number[]
    const seconds = starts.length ? await secondsSince($, Math.min(...starts)) : undefined
    const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
    const what = pending.size > 1 ? `Selecting models for ${pending.size} agents` : 'Selecting a model'
    return next({ ...e, props: { ...e.props, message: `${what}…${elapsed}`, suffix: '' } })
  })

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const original = await next(e)
    if (!seen.has(e.requestId)) {
      seen.add(e.requestId)
      // A call still routing waits for a later reply; one that failed routing is dropped.
      const ready = cards.unplaced.filter((id) => records.has(id))
      cards.unplaced = cards.unplaced.filter((id) => pending.has(id))
      if (ready.length) anchors.set(e.requestId, ready)
    }
    const ids = anchors.get(e.requestId)
    if (!showModel || !ids) return original
    const { Box } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: [...ids.map((id) => card($, e, records.get(id)!)), original] })
  })
  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    routedModels.reset()
    if (e.reason === 'clear') {
      routedDisplays.records.clear()
      routedDisplays.sessionId = undefined
      routedDisplays.root = undefined
    }
    return next(e)
  })
}
