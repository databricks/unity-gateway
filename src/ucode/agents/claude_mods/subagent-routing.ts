import type { On, PluginOptions, TurnCompleteInput } from 'claude-code'

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
  name: string; task: string; model: string; rationale: string; seconds?: number
  explanation?: RoutingExplanation
  spawned?: { agentId: string; model?: string; background: boolean }
  parentAgentId?: string
}
type ExplanationState = { enabled: boolean; showNumbers?: boolean; latest?: RoutingRecord; paneRecord?: RoutingRecord; modelId?: string }

const EXPLANATION_PANE = 'smart-router-explanation'
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

const canonicalModel = (model: string): string => {
  const original = model.replace(/^anthropic-aigw-[0-9a-fA-F]{8}-/, '')
  if (original.startsWith('system.ai.')) return original.slice('system.ai.'.length).replace(/\[1m\]$/, '')
  return original.split('.').length === 3 ? original : original.replace(/\[1m\]$/, '')
}

export const REPORT_REJECTED = 'Smart Router report rejected: use a child reference with view "status" or "result".'
const REPORT_UNAVAILABLE = 'Smart Router report unavailable: the child model could not be verified.'

function createReports() {
  const children = new Map<string, RoutingRecord>()
  const completions = new Map<string, TurnCompleteInput>()
  const handbacks = new Map<string, string>()
  const turns = new Map<string | undefined, string>()
  const reporting = new Set<string>()
  const pendingReports = new Map<string, string | undefined>()
  const pendingNotifications = new Set<string>()
  return {
    step(turnId: string, agentId?: string) {
      turns.set(agentId, turnId)
      for (const [childId, parentId] of pendingReports) {
        if (parentId === agentId) {
          pendingReports.delete(childId)
          reporting.add(turnId)
        }
      }
    },
    started(record: RoutingRecord) {
      children.set(record.spawned!.agentId, record)
      if (record.spawned!.background) pendingNotifications.add(record.spawned!.agentId)
      const turnId = turns.get(record.parentAgentId)
      if (turnId) reporting.add(turnId)
    },
    completed(event: TurnCompleteInput) {
      reporting.delete(event.turnId)
      if (event.agentId) {
        completions.set(event.agentId, {...event, answer: handbacks.get(event.agentId) ?? event.answer})
      }
    },
    handback(agentId: string, answer: string) {
      handbacks.set(agentId, answer)
      const completed = completions.get(agentId)
      if (completed) completions.set(agentId, {...completed, answer})
    },
    sending(agentId: string) {
      const child = children.get(agentId)
      const alreadyPending = pendingReports.has(agentId)
      if (child) pendingReports.set(agentId, child.parentAgentId)
      return () => { if (!alreadyPending) pendingReports.delete(agentId) }
    },
    resumed(agentId: string) {
      const completion = completions.get(agentId)
      const handback = handbacks.get(agentId)
      completions.delete(agentId); handbacks.delete(agentId)
      const child = children.get(agentId)
      return (succeeded: boolean) => {
        if (succeeded) {
          const turnId = child && turns.get(child.parentAgentId)
          if (turnId) reporting.add(turnId)
          if (child?.spawned?.background) pendingNotifications.add(agentId)
          return
        }
        if (completion && !completions.has(agentId)) completions.set(agentId, completion)
        if (handback !== undefined && !handbacks.has(agentId)) handbacks.set(agentId, handback)
      }
    },
    reset() {
      children.clear(); completions.clear(); handbacks.clear(); reporting.clear(); turns.clear()
      pendingReports.clear(); pendingNotifications.clear()
    },
    notification(turnId?: string) {
      for (const agentId of pendingNotifications) {
        const child = children.get(agentId)
        if (!completions.has(agentId) || child?.parentAgentId !== undefined) continue
        pendingNotifications.delete(agentId)
        if (turnId) reporting.add(turnId)
        else pendingReports.set(agentId, undefined)
      }
    },
    notificationContext() {
      const references = [...pendingReports].filter(([, parentId]) => parentId === undefined)
        .map(([child_id]) => ({child_id, view: completions.has(child_id) ? 'result' : 'status'}))
      return references.length ? 'Report the routed children with only this JSON: '
        + JSON.stringify(references.length === 1 ? references[0] : references)
        + '. UG renders their identity and recorded answers. Do not add prose or model names.' : undefined
    },
    active(turnId = turns.get(undefined)) { return Boolean(turnId && reporting.has(turnId)) },
    model(agentId?: string) {
      const child = agentId ? children.get(agentId) : undefined
      const resolved = child?.spawned?.model
      return child && resolved && canonicalModel(resolved) === canonicalModel(child.model)
        ? resolved : child?.model
    },
    render(text: string, parentAgentId?: string): string {
      let input: unknown
      try { input = JSON.parse(text) } catch { return REPORT_REJECTED }
      const references = Array.isArray(input) ? input : [input]
      if (!references.length || references.length > children.size) return REPORT_REJECTED
      const used = new Set<string>()
      const output: string[] = []
      for (const reference of references) {
        if (!isObject(reference) || Object.keys(reference).length !== 2
            || typeof reference.child_id !== 'string'
            || (reference.view !== 'status' && reference.view !== 'result')
            || used.has(reference.child_id)) return REPORT_REJECTED
        const child = children.get(reference.child_id)
        if (!child || child.parentAgentId !== parentAgentId) return REPORT_REJECTED
        const completion = completions.get(reference.child_id)
        const actual = completion?.usage?.model || child.spawned?.model
        if (!actual || canonicalModel(actual) !== canonicalModel(child.model)) return REPORT_UNAVAILABLE
        used.add(reference.child_id)
        const state = !completion ? 'Running' : completion.isAborted ? 'Interrupted'
          : completion.reason === 'answer' ? 'Finished' : 'Failed'
        const heading = `${child.name} · ${actual} · ${state}`
        if (reference.view === 'result') {
          if (state !== 'Finished' || !completion?.answer) return 'Smart Router report unavailable: the child has no completed answer.'
          output.push(`${heading}\n\nChild answer:\n${completion.answer.split('\n').map(line => `> ${line}`).join('\n')}`)
        } else output.push(heading)
      }
      return output.join('\n\n')
    },
  }
}

export const routingReports = createReports()

function utf8Length(value: string): number {
  let bytes = 0
  for (const char of value) {
    const point = char.codePointAt(0) ?? 0
    bytes += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4
    if (bytes > MAX_EXPLANATION_BYTES) return bytes
  }
  return bytes
}

function matchesRequestIdentity(value: unknown, sessionId?: string, toolUseId?: string): boolean {
  if (!isObject(value)) return true
  for (const source of [value, value.evidence]) {
    if (!isObject(source)) continue
    for (const [field, expected] of [['session_id', sessionId], ['tool_use_id', toolUseId]] as const) {
      const actual = source[field]
      if (actual != null && (typeof actual !== 'string' || actual.length > 256 || actual !== expected)) return false
    }
  }
  return true
}

function parseExplanation(
  value: unknown, routedModel: string, sessionId?: string, toolUseId?: string,
): RoutingExplanation | undefined {
  if (!isObject(value)) return undefined
  const serialized = JSON.stringify(value)
  if (Object.keys(value).length > 32 || utf8Length(serialized) > MAX_EXPLANATION_BYTES
      || !matchesRequestIdentity(value, sessionId, toolUseId)) return undefined
  const selectedModel = safeText(value.selected_model, 160)
  const summary = safeText(value.summary, 2000)
  if (!selectedModel || !/^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,159}$/.test(selectedModel)
      || selectedModel !== canonicalModel(routedModel) || !summary
      || !Array.isArray(value.models) || value.models.length < 1
      || !Array.isArray(value.task_understanding) || value.task_understanding.length > 8) return undefined

  const taskUnderstanding = value.task_understanding.map((item: unknown) => safeText(item, 240))
  if (taskUnderstanding.some((item: string | undefined) => !item)) return undefined
  const ids = new Set<string>()
  const models: ExplanationModel[] = []
  for (const candidate of value.models) {
    if (!isObject(candidate)) return undefined
    const id = safeText(candidate.id, 160)
    const name = safeText(candidate.name, 128)
    const reason = safeText(candidate.reason, 1200)
    if (!id || !/^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,159}$/.test(id) || ids.has(id)
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

const shortModel = (model: string): string => model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')

async function openExplanation($: any, state: ExplanationState, record: RoutingRecord): Promise<boolean> {
  state.showNumbers = false
  state.paneRecord = record
  state.modelId = record.explanation?.selectedModel
  $.ui.invalidate('ui.render')
  try {
    await $.ui.open({
      id: EXPLANATION_PANE, title: 'Why this model?', focus: true, closeOnEscape: true, rows: 18, columns: 72,
    })
    const panes = await $.ui.panes()
    if (state.paneRecord && panes.some((pane) => pane.id === EXPLANATION_PANE && pane.isPlaced)) return true
    await $.ui.close({ id: EXPLANATION_PANE })
  } catch {}
  return false
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
      ...(onWhy ? [Button({ key: 'why-this-model', label: 'Why this model?', onPress: onWhy })] : []),
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
  routingReports.reset()
  const names = new Map<string, string>()
  const unusedNames = [...ANIMALS]
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()
  // Claude folds parallel Agent rows into its own block, which no hook reaches, so a call
  // that was routing alongside another is drawn as a card above the parent's next reply.
  // A lone call keeps its decision under its own row.
  const grouped = new Set<string>()
  const records = new Map<string, RoutingRecord>()
  // The reply each batch is drawn above.
  const anchors = new Map<string, string[]>()
  // Replies already drawn before a batch was routed are never its anchor.
  const seen = new Set<string>()
  const explanationState: ExplanationState = { enabled: false }

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    const [verb, action, ...extra] = e.args.trim().split(/\s+/)
    if (verb !== 'explain') return next(e)
    if (extra.length || (action !== 'on' && action !== 'off' && action !== 'latest')) {
      return { text: 'Usage: /smart-router explain on|off|latest' }
    }
    if (action === 'on') {
      explanationState.enabled = true
      $.ui.invalidate('ui.render')
      return { text: 'Routing explanations are on for this session.' }
    }
    if (action === 'off') {
      explanationState.enabled = false
      explanationState.paneRecord = undefined
      explanationState.modelId = undefined
      $.ui.invalidate('ui.render')
      try { await $.ui.close({ id: EXPLANATION_PANE }) } catch {}
      return { text: 'Routing explanations are off for this session.' }
    }
    if (!explanationState.latest) return { text: 'No completed routing decision is available in this session.' }
    return await openExplanation($, explanationState, explanationState.latest)
      ? { text: 'Showing the latest routing explanation.' }
      : { text: 'Could not open the native routing explanation pane.' }
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
    const breakdown = (label: string, values: NumericDetails | undefined) => values && Object.keys(values).length
      ? Text({ dimColor: true, children: [`${label}: ${Object.entries(values)
        .map(([key, value]) => `${key} ${value.toFixed(3)}`).join(' · ')}`] }) : undefined
    const detailLines = [
      numberText('Quality estimate', detail.qualityEstimate),
      numberText('Est. cost', detail.estimatedCostUsd, ' USD'),
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
      ...(detailLines.length ? [Button({ key: 'numbers',
        label: explanationState.showNumbers ? 'Hide numbers' : 'Show numbers',
        onPress: () => { explanationState.showNumbers = !explanationState.showNumbers; $.ui.invalidate('ui.render') },
      }), ...(explanationState.showNumbers ? detailLines : [])] : []),
      ...(explanationState.showNumbers && explanation.formula ? [Text({ dimColor: true, children: [`Scoring formula: ${explanation.formula}`] })] : []),
      Button({ key: 'close', label: 'Close', onPress: () => $.ui.close({ id: EXPLANATION_PANE }) }),
    ] })
  })

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length || e.subagent_type === 'fork' || e.resume) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    let explanation: RoutingExplanation | undefined
    let sessionId: string | undefined
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
      explanation = undefined
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
      if (!explanation) $.ui.log('Smart Router returned invalid explanation details; keeping its selected model.')
    }
    const seconds = tool_use_id ? await secondsSince($, pending.get(tool_use_id)) : undefined
    if (tool_use_id) {
      pending.delete(tool_use_id)
      if (route) {
        const record: RoutingRecord = {
          name: names.get(tool_use_id)!,
          task: input.description || input.subagent_type || tool,
          model: route.model,
          rationale: route.rationale,
          seconds,
          explanation,
          parentAgentId: agentId,
        }
        records.set(tool_use_id, record)
        explanationState.latest = record
      }
      $.ui.invalidate('ui.render')
    }
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
      `Smart Router launch: ${identity}. Report this child using only `
      + `{"child_id":${JSON.stringify(record.spawned.agentId)},"view":"status"} or view "result" for its completed answer. `
      + 'For multiple children return an array of these references. Do not add prose, fences, names, or model fields. '
      + 'UG renders the verified identity and recorded child answer. Tool calls remain available.',
    ] }
  })

  on('agent.spawn', async ($, e, next) => {
    const route = e.resume ? undefined : records.get(e.tool_use_id)
    const name = route?.name
    const finishResume = e.resume ? routingReports.resumed(e.resume.agentId) : undefined
    let result
    try {
      result = await next(route ? { ...e, model: route.model } : e)
      finishResume?.(result.deny === undefined && Boolean(result.agentId))
    } catch (error) {
      finishResume?.(false)
      throw error
    }
    if (route && result.agentId && !result.deny) {
      route.spawned = { agentId: result.agentId, model: result.model || undefined, background: e.background }
      routingReports.started(route)
      $.ui.invalidate('ui.render')
    }
    if (name && result.agentId) {
      try {
        // The native bottom bar is outside ui.render; join its task ID to this call.
        await $.fs.write(`${$.plugin.root}/agent-names/agent-${encodeURIComponent(result.agentId)}.json`,
          JSON.stringify({ session_id: await $.session.id(), name }))
      } catch {
        $.ui.log('Could not save the subagent display name; its bottom bar will use the native label.')
      }
    }
    return result
  })

  on('tool.call', {tool: 'SubagentHandback'}, async ($, e, next) => {
    const rollback = e.agentId ? routingReports.sending(e.agentId) : undefined
    try {
      const result = await next(e)
      if (result.deny !== undefined || result.isError) rollback?.()
      else if (e.agentId && typeof e.message === 'string') routingReports.handback(e.agentId, e.message)
      return result
    } catch (error) {
      rollback?.()
      throw error
    }
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const record = records.get(e.requestId)
    const name = names.get(e.requestId)
    const original = await next(name && (record || pending.has(e.requestId)) ? {
      ...e,
      props: { ...e.props, tool: name },
    } : e)
    if (!record && !pending.has(e.requestId)) return original

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
    const onWhy = explanationState.enabled ? () => { void openExplanation($, explanationState, record) } : undefined
    return Box({ flexDirection: 'column', children: [original, ...selection($, e, record, '  ↳ ', 4, onWhy)] })
  })

  // Claude's parallel-agent block hides the row spinner, so the turn's own spinner line
  // also says routing is under way, timed from the oldest call still waiting.
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
    if (!ids) return original
    const { Box } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: [...ids.map((id) => {
      const record = records.get(id)!
      const onWhy = explanationState.enabled ? () => { void openExplanation($, explanationState, record) } : undefined
      return card($, e, record, onWhy)
    }), original] })
  })
  on('session.end', {reason: ['clear', 'resume', 'logout', 'prompt_input_exit', 'other']}, async ($, e, next) => {
    routingReports.reset()
    return next(e)
  })
}
