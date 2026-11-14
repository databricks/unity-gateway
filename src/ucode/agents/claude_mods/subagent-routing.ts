import type { On, PluginOptions } from 'claude-code'
import { parseSmartRouterCommand } from './isaac-feedback.ts'

type NumericDetails = Record<string, number>
type ExplanationModel = {
  id: string; name: string; selected: boolean; considered: boolean; reason: string
  qualityEstimate?: number; estimatedCostUsd?: number; routingScore?: number
  components?: NumericDetails; differenceFromWinner?: NumericDetails
}
type RoutingExplanation = {
  selectedModel: string; summary: string; taskUnderstanding: string[]; models: ExplanationModel[]
  recipe?: string; formula?: string
}
type RoutingRecord = {
  generation: number; toolUseId: string; name: string; task: string; model: string; rationale: string; seconds?: number
  spawned?: { agentId: string; model?: string; background: boolean }; explanation?: RoutingExplanation
}
type ExplanationState = { generation: number; openAttempt: number; enabled: boolean; showNumbers?: boolean; latest?: RoutingRecord; paneRecord?: RoutingRecord; modelId?: string }

const EXPLANATION_PANE = 'smart-router-explanation'
const EXPLANATION_OPEN_FAILURE = 'Could not open the native routing explanation pane.'
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
  const original = unwrapGatewayModel(model)
  if (original.startsWith('system.ai.')) return original.slice('system.ai.'.length).replace(/\[1m\]$/, '')
  return original.split('.').length === 3 ? original : original.replace(/\[1m\]$/, '')
}

function utf8Length(value: string): number {
  let bytes = 0
  for (const char of value) {
    const point = char.codePointAt(0) ?? 0
    bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4
    if (bytes > MAX_EXPLANATION_BYTES) return bytes
  }
  return bytes
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

function parseExplanation(
  value: unknown, routedModel: string, sessionId?: string, toolUseId?: string,
): RoutingExplanation | undefined {
  if (!isObject(value)) return undefined
  const serialized = JSON.stringify(value)
  if (Object.keys(value).length > 32 || utf8Length(serialized) > MAX_EXPLANATION_BYTES
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
  const recipe = value.recipe === undefined ? undefined : safeText(value.recipe, 240)
  const formula = value.formula === undefined ? undefined : safeText(value.formula, 500)
  if ((value.recipe !== undefined && !recipe) || (value.formula !== undefined && !formula)) return undefined
  return { selectedModel, summary, taskUnderstanding: taskUnderstanding as string[], models, recipe, formula }
}

const children = new Map<string, RoutingRecord>()
export const routedModels = {
  started(record: RoutingRecord) { children.set(record.spawned!.agentId, record) },
  model(agentId?: string) {
    const child = agentId ? children.get(agentId) : undefined
    const resolved = child?.spawned?.model
    return child && resolved && canonicalModel(resolved) === canonicalModel(child.model)
      ? resolved : child?.model
  },
  reset() { children.clear() },
}

const ANIMALS = [
  'Otter', 'Panda', 'Lynx', 'Fox', 'Badger', 'Beaver', 'Bison', 'Bobcat', 'Camel', 'Caribou',
  'Cheetah', 'Chipmunk', 'Cougar', 'Coyote', 'Deer', 'Dingo', 'Dolphin', 'Donkey', 'Elephant', 'Elk',
  'Ferret', 'Gazelle', 'Giraffe', 'Goat', 'Gopher', 'Gorilla', 'Hamster', 'Hare', 'Hedgehog', 'Hippo',
  'Horse', 'Hyena', 'Ibex', 'Impala', 'Jaguar', 'Kangaroo', 'Koala', 'Lemur', 'Leopard', 'Lion',
  'Llama', 'Marmot', 'Meerkat', 'Mink', 'Mole', 'Mongoose', 'Moose', 'Mouse', 'Narwhal', 'Ocelot',
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

async function openExplanation($: any, state: ExplanationState, record: RoutingRecord): Promise<boolean> {
  const attempt = ++state.openAttempt
  state.showNumbers = false
  state.paneRecord = record
  state.modelId = record.explanation?.selectedModel
  $.ui.invalidate('ui.render')
  try {
    const opened = await $.ui.open({
      id: EXPLANATION_PANE, title: 'Why this model?', focus: true, closeOnEscape: true, rows: 18, columns: 72,
    })
    if (state.paneRecord && (state.openAttempt !== attempt || opened.isPlaced)) return true
    await $.ui.close({ id: EXPLANATION_PANE })
  } catch {}
  if (state.openAttempt !== attempt) return Boolean(state.paneRecord)
  if (state.paneRecord === record) {
    state.paneRecord = undefined
    state.modelId = undefined
    $.ui.invalidate('ui.render')
  }
  return false
}

const whyHandler = ($: any, state: ExplanationState, record: RoutingRecord) => async () => {
  if (!state.enabled || record.generation !== state.generation) return
  if (!await openExplanation($, state, record)) $.ui.toast(EXPLANATION_OPEN_FAILURE)
}

// Claude's own spinner glyphs, played forwards then back as Claude does.
const SPINNER_GLYPHS = ['·', '✢', '✳', '✶', '✻', '✽']
const SPINNER = [...SPINNER_GLYPHS, ...SPINNER_GLYPHS.slice(1, -1).reverse()]
const SPINNER_INTERVAL_MS = 100

// The timer is cosmetic: without a clock it is omitted rather than blocking routing.
const now = async ($: any): Promise<number | undefined> => {
  try {
    return await $.clock.now()
  } catch {
    return undefined
  }
}
const secondsSince = async ($: any, start: number | undefined): Promise<number | undefined> => {
  const current = start === undefined ? undefined : await now($)
  return current === undefined || start === undefined ? undefined : Math.max(0, current - start) / 1000
}

// The selected model with its routing time, then the reason. Labels recede so the routed
// model, in the status band's ON green, stands out; the time is omitted without a clock.
const selection = (
  $: any, e: any, record: RoutingRecord, arrow: string, reasonPadding: number, onWhy?: () => void,
) => {
  const { Box, Text, Button } = $.ui.resolve(e)
  const lines = [Box({
    flexDirection: 'row',
    children: [
      Text({ dimColor: true, children: [`${arrow}Selected Model: `] }),
      Text({ color: 'green', bold: true, children: [shortModel(record.model)] }),
      ...(record.seconds === undefined ? [] : [
        Text({ dimColor: true, children: [` (routed in ${record.seconds.toFixed(1)}s)`] }),
      ]),
      ...(onWhy ? [Button({ key: `why-this-model-${record.toolUseId}`, label: 'Why this model?', onPress: onWhy })] : []),
    ],
  })]
  if (record.spawned?.model && record.spawned.model !== record.model) {
    lines.push(Box({ paddingLeft: reasonPadding,
      children: [Text({ children: [`Launched Model: ${safeText(record.spawned.model, 240)}`] })],
    }))
  }
  if (record.rationale) {
    lines.push(Box({
      paddingLeft: reasonPadding,
      children: [Text({ children: [Text({ dimColor: true, children: ['Reason: '] }), record.rationale] })],
    }))
  }
  return lines
}

// A grouped call's decision: its task, then the selection.
const card = ($: any, e: any, record: RoutingRecord, onWhy?: () => void) => {
  const { Box, Text } = $.ui.resolve(e)
  return Box({
    flexDirection: 'column',
    paddingLeft: 2,
    marginBottom: 1,
    children: [Text({ bold: true, children: [record.name, ' — ', record.task] }), ...selection($, e, record, '↳ ', 2, onWhy)],
  })
}

// Grouped calls not yet drawn, in call order. They live outside registration so the
// register module's one turn.complete handler can drop them.
const cards = { unplaced: [] as string[] }

// An aborted turn draws no reply, so its calls must not reach a later turn. A turn that
// merely ends, as when the reply waits on a background agent, keeps them.
export function endSubagentRoutingTurn(aborted: boolean): void {
  if (aborted) cards.unplaced = []
}

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  routedModels.reset()
  const names = new Map<string, string>()
  const unusedNames = [...ANIMALS]
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()
  // Claude folds parallel Agent rows into its own block, which no hook reaches, so a call
  // that was routing alongside another is drawn as a card above the parent's next reply.
  // A lone call keeps its decision under its own row.
  const grouped = new Set<string>()
  const records = new Map<string, RoutingRecord>()
  // Keep a reviewed call's selected route until its native spawn and receipt finish,
  // even when a session reset discards its display record.
  const activeRoutes = new Map<string, RoutingRecord>()
  // The reply each batch is drawn above.
  const anchors = new Map<string, string[]>()
  // Replies already drawn before a batch was routed are never its anchor.
  const seen = new Set<string>()
  const explanationState: ExplanationState = { generation: 0, openAttempt: 0, enabled: false }

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    const command = parseSmartRouterCommand(e.args)
    if (command?.action !== 'explain') return next(e)
    const action = command.explanationAction
    if (action === 'on') {
      if (!(options.routeCommand as readonly string[] | undefined)?.length) {
        return { text: 'Routing explanations are unavailable because subagent routing is not configured.' }
      }
      explanationState.enabled = true
      $.ui.invalidate('ui.render')
      return { text: 'Routing explanations are on.' }
    }
    if (action === 'off') {
      explanationState.enabled = false
      explanationState.paneRecord = undefined
      explanationState.modelId = undefined
      $.ui.invalidate('ui.render')
      try { await $.ui.close({ id: EXPLANATION_PANE }) } catch {}
      return { text: 'Routing explanations are off.' }
    }
    if (!explanationState.latest) return { text: 'No completed routing decision is available in this session.' }
    return await openExplanation($, explanationState, explanationState.latest)
      ? { text: 'Showing the latest routing explanation.' }
      : { text: EXPLANATION_OPEN_FAILURE }
  })

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== EXPLANATION_PANE || e.surface === 'mobile') return next(e)
    const { Box, Text, Button, Select } = $.ui.resolve(e)
    const record = explanationState.paneRecord
    if (!record) return next(e)
    const explanation = record.explanation
    if (!explanation) return Box({ flexDirection: 'column', children: [
      Text({ bold: true, children: ['Why this model?'] }),
      Text({ children: [`Selected route: ${shortModel(record.model)}`] }),
      ...(safeText(record.rationale, 2000) ? [Text({ children: [safeText(record.rationale, 2000)!] })] : []),
      Text({ children: ['No routing explanation details were returned. The selected model is unchanged.'] }),
      Button({ key: 'close', label: 'Close', onPress: () => $.ui.close({ id: EXPLANATION_PANE }) }),
    ] })

    const selected = explanation.models.find((model) => model.id === explanationState.modelId)
    const detail = selected ?? explanation.models.find((model) => model.selected)!
    const numberText = (label: string, value: number | undefined, suffix = '') => value === undefined
      ? undefined : Text({ children: [`${label}: ${value.toFixed(3)}${suffix}`] })
    const cost = detail.estimatedCostUsd
    const costText = cost === undefined ? undefined
      : Text({ children: [`Est. cost: ${cost > 0 && cost < 1e-6
        ? '<0.000001' : cost.toFixed(6).replace(/\.?0+$/, '')} USD`] })
    const breakdown = (label: string, values: NumericDetails | undefined) => values && Object.keys(values).length
      ? Text({ dimColor: true, children: [`${label}: ${Object.entries(values)
        .map(([key, value]) => `${key} ${value.toFixed(3)}`).join(' · ')}`] }) : undefined
    const detailLines = [
      numberText('Quality estimate', detail.qualityEstimate),
      costText,
      numberText('Routing score', detail.routingScore),
      breakdown('Components', detail.components),
      breakdown('Vs. routed', detail.differenceFromWinner),
    ].filter(Boolean)
    return Box({ flexDirection: 'column', children: [
      Text({ color: 'green', bold: true, children: [`Chosen: ${explanation.models.find((model) => model.selected)!.name}`] }),
      Text({ children: [explanation.summary] }),
      ...(explanation.taskUnderstanding.length ? [
        Text({ bold: true, children: ['Task understanding'] }),
        ...explanation.taskUnderstanding.map((item) => Text({ children: [item] })),
      ] : []),
      Box({ marginTop: 1, marginBottom: 1, children: [Select({
        key: 'compare-model', label: 'Compare model', autoFocus: true,
        value: detail.id,
        options: explanation.models.map((model) => ({ value: model.id,
          label: `${model.name}${model.selected ? ' · chosen' : ''}` })),
        onSelect: (id) => { explanationState.modelId = id; $.ui.invalidate('ui.render') },
      })] }),
      Text({ bold: true, children: [`${detail.name} — ${detail.considered ? 'considered' : 'not considered'}`] }),
      Text({ children: [detail.reason] }),
      ...(detailLines.length || explanation.formula ? [Button({ key: 'numbers',
        label: explanationState.showNumbers ? 'Hide numbers' : 'Show numbers',
        onPress: () => { explanationState.showNumbers = !explanationState.showNumbers; $.ui.invalidate('ui.render') },
      }), ...(explanationState.showNumbers ? detailLines : [])] : []),
      ...(explanationState.showNumbers && explanation.formula ? [Text({ dimColor: true, children: [`Scoring formula: ${explanation.formula}`] })] : []),
      Button({ key: 'close', label: 'Close', onPress: () => $.ui.close({ id: EXPLANATION_PANE }) }),
    ] })
  })

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[] | undefined
    if (!command?.length || e.subagent_type === 'fork' || e.resume) return next(e)
    const generation = explanationState.generation
    const { tool, tool_use_id, agentId: _agentId, ...input } = e
    let route
    let explanation: RoutingExplanation | undefined
    let sessionId: string | undefined
    let spinner: { cancel: () => void } | undefined
    let name = ''
    let startedAt: number | undefined
    if (tool_use_id) {
      if (!names.has(tool_use_id)) {
        if (!unusedNames.length) unusedNames.push(...ANIMALS)
        const animal = unusedNames.splice(Math.floor(Math.random() * unusedNames.length), 1)[0]
        const cycle = Math.floor(names.size / ANIMALS.length)
        names.set(tool_use_id, cycle ? `${animal} ${cycle + 1}` : animal)
      }
      name = names.get(tool_use_id)!
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
      startedAt = await now($)
      if (generation === explanationState.generation) pending.set(tool_use_id, startedAt)
      // Mods have no global timers; $.clock redraws the spinner while the router answers.
      // The frame comes from the clock, so parallel calls do not speed it up.
      try {
        spinner = $.clock.every(SPINNER_INTERVAL_MS, () => {
          if (generation === explanationState.generation) $.ui.invalidate('ui.render')
        })
      } catch {}
      if (generation === explanationState.generation) $.ui.invalidate('ui.render')
    }
    try {
      sessionId = await $.session.id()
      const result = await $.process.run(command, {
        stdin: JSON.stringify({ session_id: sessionId, tool_use_id, tool_name: tool, tool_input: input }),
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
    if (route?.explanation !== undefined) {
      try {
        explanation = matchesRequestIdentity(route, sessionId, tool_use_id)
          ? parseExplanation(route.explanation, route.model, sessionId, tool_use_id) : undefined
      } catch {
        // Optional display metadata must never discard a valid model selection.
      }
      if (!explanation) $.ui.log('Smart Router returned invalid explanation details; keeping its selected model.', { to: 'debug' })
    }
    const seconds = await secondsSince($, startedAt)
    let record: RoutingRecord | undefined
    if (tool_use_id) {
      if (generation === explanationState.generation) pending.delete(tool_use_id)
      if (route) {
        record = {
          generation,
          toolUseId: tool_use_id,
          name,
          task: input.description || input.subagent_type || tool,
          model: route.model,
          rationale: route.rationale,
          seconds,
          explanation,
        }
        activeRoutes.set(tool_use_id, record)
        if (generation === explanationState.generation) {
          records.set(tool_use_id, record)
          explanationState.latest = record
        }
      }
      if (generation === explanationState.generation) $.ui.invalidate('ui.render')
    }
    // Auto mode reviewed this input; model identity belongs in the result, not its arguments.
    try {
      const result = await next(e)
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
    } finally {
      if (tool_use_id && activeRoutes.get(tool_use_id) === record) activeRoutes.delete(tool_use_id)
    }
  })

  on('agent.spawn', async ($, e, next) => {
    const route = e.resume ? undefined : activeRoutes.get(e.tool_use_id) ?? records.get(e.tool_use_id)
    const name = route?.name
    const result = await next(route ? { ...e, model: route.model } : e)
    if (route && result.agentId && !result.deny) {
      route.spawned = { agentId: result.agentId, model: result.model || undefined, background: e.background }
      routedModels.started(route)
      if (records.get(e.tool_use_id) === route) $.ui.invalidate('ui.render')
    }
    if (name && result.agentId && records.get(e.tool_use_id) === route) {
      try {
        // The native bottom bar is outside ui.render; join its task ID to this call.
        const sessionId = await $.session.id()
        if (records.get(e.tool_use_id) === route) {
          await $.fs.write(`${$.plugin.root}/agent-names/agent-${encodeURIComponent(result.agentId)}.json`,
            JSON.stringify({ session_id: sessionId, name }))
        }
      } catch {
        $.ui.log('Could not save the subagent display name; its bottom bar will use the native label.')
      }
    }
    return result
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const generation = explanationState.generation
    const record = records.get(e.requestId)
    const name = names.get(e.requestId)
    const original = await next(name && (record || pending.has(e.requestId)) ? {
      ...e,
      props: { ...e.props, tool: name },
    } : e)
    if (generation !== explanationState.generation) return original
    if (!record && !pending.has(e.requestId)) return original

    const { Box, Text } = $.ui.resolve(e)
    if (!record) {
      const glyph = SPINNER[Math.floor(((await now($)) ?? 0) / SPINNER_INTERVAL_MS) % SPINNER.length]
      const seconds = await secondsSince($, pending.get(e.requestId))
      if (generation !== explanationState.generation) return original
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
    const onWhy = explanationState.enabled ? whyHandler($, explanationState, record) : undefined
    return Box({ flexDirection: 'column', children: [original, ...selection($, e, record, '  ↳ ', 4, onWhy)] })
  })

  // Claude's parallel-agent block hides the row spinner, so the turn's own spinner line
  // also says routing is under way, timed from the oldest call still waiting.
  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    const generation = explanationState.generation
    if (!pending.size) return next(e)
    const starts = [...pending.values()].filter((start) => start !== undefined) as number[]
    const seconds = starts.length ? await secondsSince($, Math.min(...starts)) : undefined
    if (generation !== explanationState.generation) return next(e)
    const elapsed = seconds === undefined ? '' : ` ${Math.floor(seconds)}s`
    const what = pending.size > 1 ? `Selecting models for ${pending.size} agents` : 'Selecting a model'
    return next({ ...e, props: { ...e.props, message: `${what}…${elapsed}`, suffix: '' } })
  })

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const generation = explanationState.generation
    const original = await next(e)
    if (generation !== explanationState.generation) return original
    if (!seen.has(e.requestId)) {
      seen.add(e.requestId)
      // A call still routing waits for a later reply; one that failed routing is dropped.
      const ready = cards.unplaced.filter((id) => records.has(id))
      cards.unplaced = cards.unplaced.filter((id) => pending.has(id))
      if (ready.length) anchors.set(e.requestId, ready)
    }
    const ids = anchors.get(e.requestId)
    if (!ids) return original
    const { Box } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: [...ids.map((id) => {
      const record = records.get(id)!
      const onWhy = explanationState.enabled ? whyHandler($, explanationState, record) : undefined
      return card($, e, record, onWhy)
    }), original] })
  })
  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    explanationState.generation++
    pending.clear()
    records.clear()
    names.clear()
    unusedNames.splice(0, unusedNames.length, ...ANIMALS)
    grouped.clear()
    anchors.clear()
    seen.clear()
    cards.unplaced = []
    routedModels.reset()
    explanationState.latest = undefined
    explanationState.paneRecord = undefined
    explanationState.modelId = undefined
    $.ui.invalidate('ui.render')
    try { await $.ui.close({ id: EXPLANATION_PANE }) } catch {}
    return next(e)
  })
}
