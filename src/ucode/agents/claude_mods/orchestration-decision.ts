import type {
  AgentInfo,
  EngineInterface,
  On,
  PluginOptions,
  PromptSubmitInput,
} from 'claude-code'
import { DISABLED_RECIPE, RECIPE_FIELD, SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts'
import { mergeExtraBody } from './extra-body.ts'
import { isRoutingEnabled } from './routing-state.ts'
import { armStatusBandForPrompt } from './smart-routing-status.ts'

const POLICY_VERSION = 'orchestration_v1'
const POLICY_SHA256 = 'adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa'
const CLASSIFIER_ID = 'orchestration_v1/classifier'
const ROOT_ORIGINS = new Set(['composer', 'bridge', 'sdk'])
const DISPATCH_VALUES = new Set(['work_directly', 'spawn_subagent', 'reuse_subagent'])
const DISPATCH_STATUSES = new Set([
  'undecided', 'awaiting_dispatch', 'started', 'completed', 'failed',
])
const OWNER_STATES = new Set(['running', 'completed', 'failed'])

type Owner = { agent_id: string; scope: string; state: 'running' | 'completed' | 'failed' }
type Current = {
  request_id: string
  input_sha256: string | null
  policy_version: string
  policy_sha256: string
  decision_status: 'undecided' | 'ok'
  ownership: 'main' | 'subagent' | null
  dispatch: 'work_directly' | 'spawn_subagent' | 'reuse_subagent' | null
  owner_agent_id: string | null
  turn_id?: string
  agent_tool_use_id?: string
  continuation_turn_id?: string
  continuation_prompt_id?: string
  dispatch_status: 'undecided' | 'awaiting_dispatch' | 'started' | 'completed' | 'failed'
}
type OwnershipState = {
  schema_version: 1
  session_id: string
  generation: number
  current: Current | null
  owners: Owner[]
}
type PendingPrompt = {
  queueId: string
  sessionId: string
  requestId?: string
  prompt: string
  originKind: PromptSubmitInput['origin']['kind']
}
export type OwnershipStatusCell = { value?: string }

function ownershipStatusText(
  state: OwnershipState | null,
  active: boolean,
  hasOwnershipOptions: boolean,
): string | undefined {
  const current = state?.current
  if (!hasOwnershipOptions || !active || !current || current.decision_status !== 'ok' ||
    !['started', 'completed'].includes(current.dispatch_status)) return undefined
  if (current.ownership === 'main' && current.dispatch === 'work_directly') {
    const owner = current.owner_agent_id
      ? state?.owners.find((item) => item.agent_id === current.owner_agent_id)
      : undefined
    if (owner?.state === 'running') return 'Main agent is spawning subagents.'
    return 'Main agent is not spawning subagents and is working on the task directly.'
  }
  if (current.ownership === 'subagent' &&
    (current.dispatch === 'spawn_subagent' || current.dispatch === 'reuse_subagent') && current.owner_agent_id) {
    return 'Main agent is spawning subagents.'
  }
  return undefined
}

const hasExactKeys = (value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []): boolean => {
  const keys = Object.keys(value)
  return required.every((key) => key in value) && keys.every((key) => required.includes(key) || optional.includes(key))
}
const isRecord = (value: unknown): value is Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const isString = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
function turnStartOriginKind(event: unknown): string | undefined {
  if (!isRecord(event) || !isRecord(event.origin) || typeof event.origin.kind !== 'string') return undefined
  return event.origin.kind
}
export function shouldGrantNotificationContinuation(
  eventOriginKind: string | undefined,
  pendingOriginKind: string | undefined,
  hasOpposingRootPrompt: boolean,
): boolean {
  return eventOriginKind === 'task-notification' &&
    (pendingOriginKind === undefined || pendingOriginKind === 'task-notification') &&
    !hasOpposingRootPrompt
}
export function successfulNativeOwnerResume(response: unknown, expectedAgentId: string): boolean {
  if (!isRecord(response) || response.isError === true || !isRecord(response.result)) return false
  return response.result.success === true && response.result.resumedAgentId === expectedAgentId
}
const isSha256 = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const parseObject = (text: string, label: string): Record<string, any> => {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`${label} is not valid JSON`)
  }
  if (!isRecord(parsed)) throw new Error(`${label} must be a JSON object`)
  return parsed
}
type OwnershipError = Error & { usage_receipt?: Record<string, unknown> | null }
function validateUsageReceipt(value: unknown, requestId: unknown): Record<string, unknown> {
  if (!isRecord(value) || !hasExactKeys(value, [
    'operation', 'request_id', 'classifier_id', 'provider_request_id', 'usage', 'finish_reason', 'status',
  ], ['failure_reason', 'provider_model', 'response_sha256']) || value.operation !== 'orchestration_decision' || value.request_id !== requestId ||
    value.classifier_id !== CLASSIFIER_ID ||
    ('failure_reason' in value && (!isString(value.failure_reason) || value.failure_reason.length > 256)) ||
    (value.provider_model != null && !isString(value.provider_model)) ||
    (value.response_sha256 != null && (typeof value.response_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.response_sha256))) ||
    (value.provider_request_id !== null && !isString(value.provider_request_id)) ||
    (value.usage !== null && !isRecord(value.usage)) ||
    (value.finish_reason !== null && typeof value.finish_reason !== 'string') ||
    !['ok', 'unavailable', 'truncated', 'cancelled', 'error'].includes(value.status)) {
    throw new Error('ownership usage_receipt is invalid or belongs to another request')
  }
  return value
}
const jsonText = (value: unknown): string => {
  try { return JSON.stringify(value) ?? '' } catch { return '' }
}

async function ownershipRoutingActive($: EngineInterface, hasOwnershipOptions: boolean): Promise<boolean> {
  if (!hasOwnershipOptions) return false
  const [full, subagentOnly, orchestrator] = await Promise.all([
    $.env.get('ENABLE_SMART_ROUTING_V2'),
    $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
  ])
  return orchestrator === '1' && isRoutingEnabled({
    [SMART_ROUTING_KEY]: full,
    [SUBAGENT_ONLY_KEY]: subagentOnly,
  })
}

function validateState(value: unknown, sessionId: string): OwnershipState {
  if (!isRecord(value) || !hasExactKeys(value, ['schema_version', 'session_id', 'generation', 'current', 'owners'])) {
    throw new Error('ownership state has an invalid top-level shape')
  }
  if (value.schema_version !== 1 || value.session_id !== sessionId || !Number.isSafeInteger(value.generation) || value.generation < 0) {
    throw new Error('ownership state identity or generation is invalid')
  }
  if (!Array.isArray(value.owners)) throw new Error('ownership state owners must be an array')
  const seen = new Set<string>()
  const owners: Owner[] = value.owners.map((owner: unknown) => {
    if (!isRecord(owner) || !hasExactKeys(owner, ['agent_id', 'scope', 'state'])) {
      throw new Error('ownership state contains an invalid owner')
    }
    if (!isString(owner.agent_id) || !isString(owner.scope) || !OWNER_STATES.has(owner.state) || seen.has(owner.agent_id)) {
      throw new Error('ownership state contains an invalid or duplicate owner')
    }
    seen.add(owner.agent_id)
    return { agent_id: owner.agent_id, scope: owner.scope, state: owner.state }
  })
  let current: Current | null = null
  if (value.current !== null) {
    const item = value.current
    if (!isRecord(item) || !hasExactKeys(
      item,
      ['request_id', 'input_sha256', 'policy_version', 'policy_sha256', 'decision_status', 'ownership', 'dispatch', 'owner_agent_id', 'dispatch_status'],
      ['turn_id', 'agent_tool_use_id', 'continuation_turn_id', 'continuation_prompt_id'],
    )) throw new Error('ownership state current record has an invalid shape')
    const undecided = item.decision_status === 'undecided'
    if (!isString(item.request_id) ||
      !(isSha256(item.input_sha256) || (undecided && item.input_sha256 === null)) ||
      item.policy_version !== POLICY_VERSION || item.policy_sha256 !== POLICY_SHA256 ||
      !['undecided', 'ok'].includes(item.decision_status) ||
      ![null, 'main', 'subagent'].includes(item.ownership) ||
      !(item.dispatch === null || DISPATCH_VALUES.has(item.dispatch)) ||
      !(item.owner_agent_id === null || isString(item.owner_agent_id)) ||
      !DISPATCH_STATUSES.has(item.dispatch_status) ||
      (undecided && (item.ownership !== null || item.dispatch !== null || item.owner_agent_id !== null || item.dispatch_status !== 'undecided')) ||
      ('turn_id' in item && item.turn_id !== undefined && !isString(item.turn_id)) ||
      ('agent_tool_use_id' in item && item.agent_tool_use_id !== undefined && !isString(item.agent_tool_use_id)) ||
      ('continuation_turn_id' in item && item.continuation_turn_id !== undefined && !isString(item.continuation_turn_id)) ||
      ('continuation_prompt_id' in item && item.continuation_prompt_id !== undefined && !isString(item.continuation_prompt_id))) {
      throw new Error('ownership state current record contains invalid values')
    }
    current = item as Current
  }
  return {
    schema_version: 1,
    session_id: value.session_id,
    generation: value.generation,
    current,
    owners,
  }
}

function validatePrepareEnvelope(value: unknown, requestId: string): { requestId: string; instructions: string } {
  if (!isRecord(value) || !hasExactKeys(value, ['request_id', 'instructions', 'policy_sha256'])) {
    throw new Error('ownership bridge response has an invalid shape')
  }
  if (value.request_id !== requestId || value.policy_sha256 !== POLICY_SHA256 ||
    typeof value.instructions !== 'string' || !value.instructions.trim()) {
    throw new Error('ownership bridge request, policy, or instruction binding is invalid')
  }
  return { requestId, instructions: value.instructions }
}

function directOwners(agents: readonly AgentInfo[]): AgentInfo[] {
  return agents.filter((agent) => agent.parentId === undefined && agent.type !== 'teammate' && agent.spawnedBy === undefined)
}

export function availableOwnersForDecision(
  persistedOwners: readonly Owner[],
  agents: readonly AgentInfo[],
): Owner[] {
  return persistedOwners.flatMap((owner) => {
    const actual = agents.find((agent) => agent.id === owner.agent_id && agent.description === owner.scope)
    if (!actual) return owner.state === 'completed' ? [owner] : []
    const status = ownerState(actual.status)
    return status === 'failed' ? [] : [{ agent_id: actual.id, scope: actual.description, state: status }]
  })
}

function ownerState(status: string): Owner['state'] {
  if (status === 'running') return 'running'
  if (status === 'completed') return 'completed'
  return 'failed'
}

function nextRequestId(sessionId: string): string {
  requestCounter += 1
  return `${sessionId}:${Date.now().toString(36)}:${requestCounter.toString(36)}:${Math.random().toString(36).slice(2)}`
}
let requestCounter = 0

type OwnershipOptions = {
  ownershipCommand: readonly string[] | undefined
  ownershipPolicyVersion: unknown
  ownershipPolicySha256: unknown
  ownershipAnswerDelivery: unknown
  hasOwnershipOptions: boolean
}
type OwnershipRuntime = OwnershipOptions & {
  writeQueues: Map<string, Promise<void>>
  setStatus: (state: OwnershipState | null) => void
}

async function failInitialDispatchIfIdle(
  $: EngineInterface,
  options: OwnershipRuntime,
  nativeToolCalls: Map<string, { requestId: string | null; initialDispatch: boolean }>,
  sessionId: string,
  requestId: string,
  toolUseId: string,
): Promise<void> {
  const anotherDispatchIsRunning = [...nativeToolCalls].some(([otherToolUseId, call]) =>
    otherToolUseId !== toolUseId && call.requestId === requestId && call.initialDispatch)
  if (anotherDispatchIsRunning) return
  await updateState($, options, sessionId, (latest) =>
    latest.current?.request_id === requestId && latest.current.dispatch === 'spawn_subagent' &&
      latest.current.dispatch_status === 'awaiting_dispatch'
      ? { ...latest, current: { ...latest.current, dispatch_status: 'failed' } }
      : null)
}

function requireOwnershipOptions(options: OwnershipOptions): void {
  if (!options.hasOwnershipOptions || options.ownershipPolicyVersion !== POLICY_VERSION ||
    options.ownershipPolicySha256 !== POLICY_SHA256 || typeof options.ownershipAnswerDelivery !== 'boolean') {
    throw new Error('native ownership launch options or policy binding are missing')
  }
}

type RoutingEnvironment = Record<string, string | undefined>

async function readRoutingEnvironment($: EngineInterface): Promise<RoutingEnvironment> {
  return {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
    ENABLE_SMART_ROUTER_ORCHESTRATOR: await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR'),
    CLAUDE_CODE_EXTRA_BODY: await $.env.get('CLAUDE_CODE_EXTRA_BODY'),
  }
}

async function writeRoutingEnvironment($: EngineInterface, environment: RoutingEnvironment): Promise<void> {
  // Claude's capability scanner requires literal environment-variable names.
  await $.env.set('ENABLE_SMART_ROUTING_V2', environment.ENABLE_SMART_ROUTING_V2)
  await $.env.set('ENABLE_SMART_ROUTING_SUBAGENT_ONLY', environment.ENABLE_SMART_ROUTING_SUBAGENT_ONLY)
  await $.env.set('ENABLE_SMART_ROUTER_ORCHESTRATOR', environment.ENABLE_SMART_ROUTER_ORCHESTRATOR)
  await $.env.set('CLAUDE_CODE_EXTRA_BODY', environment.CLAUDE_CODE_EXTRA_BODY)
}

async function initializeRoutingSession($: EngineInterface, options: PluginOptions): Promise<void> {
  if (!isRoutingEnabled(options)) return
  const previous = await readRoutingEnvironment($)
  const enabled = isRoutingEnabled(previous)
  const unset = Array.isArray(options.unset) ? options.unset as readonly string[] : []
  const original = (key: string): string | undefined =>
    unset.includes(key) ? undefined : options[key] as string | undefined
  const recipe = enabled ? options.recipe : DISABLED_RECIPE
  if (typeof recipe !== 'string' || /[\r\n]/.test(recipe)) {
    throw new Error('Smart Router recipe must be a single-line string.')
  }
  const environment: RoutingEnvironment = {
    ENABLE_SMART_ROUTING_V2: enabled ? original('ENABLE_SMART_ROUTING_V2') : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: enabled ? original('ENABLE_SMART_ROUTING_SUBAGENT_ONLY') : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: enabled ? original('ENABLE_SMART_ROUTER_ORCHESTRATOR') : '0',
    CLAUDE_CODE_EXTRA_BODY: mergeExtraBody(previous.CLAUDE_CODE_EXTRA_BODY, { [RECIPE_FIELD]: recipe }),
  }
  try {
    await writeRoutingEnvironment($, environment)
  } catch (error) {
    await writeRoutingEnvironment($, previous)
    throw error
  }
}

function initialState(sessionId: string): OwnershipState {
  return { schema_version: 1, session_id: sessionId, generation: 0, current: null, owners: [] }
}

async function runBridge(
  $: EngineInterface,
  options: OwnershipOptions,
  envelope: Record<string, unknown>,
  timeoutMs = 25000,
): Promise<Record<string, any>> {
  requireOwnershipOptions(options)
  const result = await $.process.run([...(options.ownershipCommand ?? [])], {
    stdin: JSON.stringify(envelope), timeoutMs,
  })
  if (result.exitCode !== 0) {
    const payload = parseObject(result.stdout.trim(), 'native ownership bridge error')
    if (!hasExactKeys(payload, ['error'], ['usage_receipt']) || !isString(payload.error)) {
      throw new Error('native ownership bridge error has an invalid shape')
    }
    const request = envelope.request
    const requestId = isRecord(request) ? request.request_id : undefined
    const failure = new Error(payload.error) as OwnershipError
    if ('usage_receipt' in payload) {
      if (payload.usage_receipt === null) failure.usage_receipt = null
      else {
        if (!isString(requestId)) throw new Error('ownership bridge returned a receipt without a decision request')
        failure.usage_receipt = validateUsageReceipt(payload.usage_receipt, requestId)
      }
    }
    throw failure
  }
  return parseObject(result.stdout.trim(), 'native ownership bridge response')
}

async function readState($: EngineInterface, options: OwnershipOptions, sessionId: string): Promise<OwnershipState> {
  const response = await runBridge($, options, { operation: 'state_read', session_id: sessionId }, 10000)
  if (!hasExactKeys(response, ['state'])) throw new Error('state_read response has an invalid shape')
  return response.state === null ? initialState(sessionId) : validateState(response.state, sessionId)
}

async function writeState($: EngineInterface, options: OwnershipOptions, state: OwnershipState): Promise<OwnershipState> {
  const response = await runBridge($, options, { operation: 'state_write', session_id: state.session_id, state }, 10000)
  if (!hasExactKeys(response, ['state'])) throw new Error('state_write response has an invalid shape')
  const saved = validateState(response.state, state.session_id)
  if (jsonText(saved) !== jsonText(state)) throw new Error('state_write response differs from the requested state')
  return saved
}

async function serialize<T>(
  queues: Map<string, Promise<void>>,
  sessionId: string,
  operation: () => Promise<T>,
): Promise<T> {
  const previous = queues.get(sessionId) ?? Promise.resolve()
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const queued = previous.then(() => gate)
  queues.set(sessionId, queued)
  await previous
  try {
    return await operation()
  } finally {
    release()
    if (queues.get(sessionId) === queued) queues.delete(sessionId)
  }
}

async function updateState(
  $: EngineInterface,
  options: OwnershipRuntime,
  sessionId: string,
  update: (state: OwnershipState) => OwnershipState | null,
): Promise<OwnershipState> {
  return serialize(options.writeQueues, sessionId, async () => {
    while (true) {
      const prior = await readState($, options, sessionId)
      const next = update(prior)
      if (!next) {
        options.setStatus(prior)
        return prior
      }
      next.generation = prior.generation + 1
      try {
        const saved = await writeState($, options, next)
        options.setStatus(saved)
        return saved
      } catch (error) {
        if (!(error instanceof Error) ||
          !/ownership state generation (?:is stale|conflicts with stored state)/.test(error.message)) throw error
      }
    }
  })
}

function reconciledState(state: OwnershipState, agents: readonly AgentInfo[]): OwnershipState {
  const owners = state.owners.map((owner) => {
    const agent = agents.find((item) => item.id === owner.agent_id)
    return agent ? { agent_id: agent.id, scope: agent.description, state: ownerState(agent.status) } : owner
  })
  const current = state.current
  if (!current?.owner_agent_id || !['started', 'completed'].includes(current.dispatch_status)) {
    return { ...state, owners }
  }
  const agent = agents.find((item) => item.id === current.owner_agent_id)
  if (!agent) return { ...state, owners }
  const status = ownerState(agent.status)
  const dispatchStatus = status === 'running' ? 'started' : status
  return { ...state, owners, current: { ...current, dispatch_status: dispatchStatus } }
}

async function reconcileOwnershipOnSessionStart(
  $: EngineInterface,
  options: PluginOptions,
  statusCell: OwnershipStatusCell,
): Promise<void> {
  const ownershipCommand = options.ownershipCommand
  const hasOwnershipOptions = Array.isArray(ownershipCommand) && ownershipCommand.length > 0
  if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
    statusCell.value = undefined
    return
  }
  const runtime: OwnershipRuntime = {
    ownershipCommand: Array.isArray(ownershipCommand) ? ownershipCommand as readonly string[] : undefined,
    ownershipPolicyVersion: options.ownershipPolicyVersion,
    ownershipPolicySha256: options.ownershipPolicySha256,
    ownershipAnswerDelivery: options.ownershipAnswerDelivery,
    hasOwnershipOptions,
    writeQueues: new Map(),
    setStatus: (state) => {
      statusCell.value = ownershipStatusText(state, true, hasOwnershipOptions)
    },
  }
  const sessionId = await $.session.id()
  const agents = directOwners(await $.agent.list())
  const state = await updateState($, runtime, sessionId, (latest) => {
    const next = reconciledState(latest, agents)
    return jsonText(next) === jsonText(latest) ? null : next
  })
  statusCell.value = ownershipStatusText(state, true, hasOwnershipOptions)
}

async function prepareIntent(
  $: EngineInterface,
  options: OwnershipOptions,
  sessionId: string,
  prompt: string,
  extra: readonly string[],
  attachments: PromptSubmitInput['attachments'],
  origin: PromptSubmitInput['origin'],
): Promise<{ requestId: string; instructions: string }> {
  requireOwnershipOptions(options)
  const state = await readState($, options, sessionId)
  const agents = directOwners(await $.agent.list())
  const tools = await $.tool.list()
  const requestId = nextRequestId(sessionId)
  // Completed native agents remain addressable by SendMessage across a resume,
  // even when this process's agent roster no longer includes them.
  const owners = availableOwnersForDecision(state.owners, agents)
  const routingEnabled = await ownershipRoutingActive($, options.hasOwnershipOptions)
  if (!routingEnabled) throw new Error('native ownership routing is disabled')
  const rootSession = ROOT_ORIGINS.has(origin.kind)
  const request = {
    schema_version: 1,
    policy_version: POLICY_VERSION,
    request_id: requestId,
    task: {
      prompt,
      context: extra.map((content) => ({ role: 'system' as const, content })),
      context_complete: false,
      attachments: (attachments ?? []).map((item) => ({
        name: item.filename || item.mediaType || item.type,
        available: false,
      })),
    },
    constraints: {
      root_session: rootSession,
      subagents_allowed: tools.some((tool) => tool.name === 'Agent' || tool.name === 'Task'),
      externally_assigned: !rootSession,
      routing_enabled: routingEnabled,
      answer_delivery: options.ownershipAnswerDelivery === true,
      available_tools: tools.map((tool) => tool.name),
    },
    existing_owners: owners,
  }
  const response = await runBridge($, options, { operation: 'prepare', session_id: sessionId, request })
  return validatePrepareEnvelope(response, requestId)
}

function undecidedRecord(requestId: string, turnId?: string): Current {
  return {
    request_id: requestId,
    input_sha256: null,
    policy_version: POLICY_VERSION,
    policy_sha256: POLICY_SHA256,
    decision_status: 'undecided',
    ownership: null,
    dispatch: null,
    owner_agent_id: null,
    ...(turnId ? { turn_id: turnId } : {}),
    dispatch_status: 'undecided',
  }
}

export function registerOrchestrationDecision(
  on: On,
  options: PluginOptions,
  statusCell: OwnershipStatusCell,
): void {
  const ownershipCommand = options.ownershipCommand
  const policyVersion = options.ownershipPolicyVersion
  const policyHash = options.ownershipPolicySha256
  const answerDelivery = options.ownershipAnswerDelivery
  const hasOwnershipOptions = Array.isArray(ownershipCommand) && ownershipCommand.length > 0
  const pending = new Map<string, PendingPrompt>()
  const pendingOrder: string[] = []
  const nativeToolCalls = new Map<string, { requestId: string | null; initialDispatch: boolean }>()
  const writeQueues = new Map<string, Promise<void>>()
  let routingActive = false
  let sessionGeneration = 0
  let answerFeedbackRequestId: string | undefined
  const setStatus = (state: OwnershipState | null): void => {
    statusCell.value = ownershipStatusText(state, routingActive, hasOwnershipOptions)
  }

  const bridgeOptions: OwnershipRuntime = {
    ownershipCommand: Array.isArray(ownershipCommand) ? ownershipCommand as readonly string[] : undefined,
    ownershipPolicyVersion: policyVersion,
    ownershipPolicySha256: policyHash,
    ownershipAnswerDelivery: answerDelivery,
    hasOwnershipOptions,
    writeQueues,
    setStatus,
  }
  const pendingTurnInitializations = new Map<string, Promise<void>>()
  const waitForTurnInitialization = async (turnId?: string): Promise<boolean> => {
    const pendingInitializations = turnId === undefined
      ? [...pendingTurnInitializations.values()]
      : [pendingTurnInitializations.get(turnId)].filter((value): value is Promise<void> => value !== undefined)
    try {
      await Promise.all(pendingInitializations)
      return true
    } catch {
      return false
    }
  }
  const clearInactive = (): void => {
    sessionGeneration += 1
    routingActive = false
    statusCell.value = undefined
    pending.clear()
    pendingOrder.length = 0
    pendingTurnInitializations.clear()
    nativeToolCalls.clear()
    answerFeedbackRequestId = undefined
  }
  const addPending = (value: PendingPrompt): void => {
    pending.set(value.queueId, value)
    pendingOrder.push(value.queueId)
  }
  const takePending = (prompt: string, originKind: string | undefined): PendingPrompt | undefined => {
    // turn.start carries no origin in the pinned public API; only infer it from one exact submit match.
    const matches = pendingOrder.filter((queueId) => {
      const item = pending.get(queueId)
      return item?.prompt === prompt && (originKind === undefined || item.originKind === originKind)
    })
    if (matches.length > 1) throw new Error('ambiguous queued ownership prompts cannot initialize a native turn')
    if (matches.length === 0) return undefined
    const [queueId] = matches
    const index = pendingOrder.indexOf(queueId)
    if (index < 0) return undefined
    pendingOrder.splice(index, 1)
    const value = pending.get(queueId)
    pending.delete(queueId)
    return value
  }
  const hasOpposingRootPrompt = (prompt: string): boolean => pendingOrder.some((queueId) => {
    const item = pending.get(queueId)
    return item?.prompt === prompt && item.originKind !== 'task-notification'
  })
  const removePending = (value: PendingPrompt): void => {
    pending.delete(value.queueId)
    const index = pendingOrder.indexOf(value.queueId)
    if (index >= 0) pendingOrder.splice(index, 1)
  }
  const deny = (message: string) => ({ deny: message })

  on('session.start', async ($, e, next) => {
    await initializeRoutingSession($, options)
    await reconcileOwnershipOnSessionStart($, options, statusCell)
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    armStatusBandForPrompt()
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return next(e)
    }
    if (e.origin.kind === 'task-notification') {
      const sessionId = await $.session.id()
      const item: PendingPrompt = {
        queueId: nextRequestId(sessionId),
        sessionId,
        prompt: e.text,
        originKind: e.origin.kind,
      }
      addPending(item)
      try {
        const entered = await next(e)
        if (isRecord(entered) && typeof entered.drop === 'string') removePending(item)
        return entered
      } catch (error) {
        removePending(item)
        throw error
      }
    }
    if (!ROOT_ORIGINS.has(e.origin.kind)) return next(e)
    routingActive = true
    try {
      requireOwnershipOptions(bridgeOptions)
      const sessionId = await $.session.id()
      const result = await prepareIntent($, bridgeOptions, sessionId, e.text, e.context ?? [], e.attachments, e.origin)
      const epoch = sessionGeneration
      if (e.turnId === undefined) {
        const current = undecidedRecord(result.requestId)
        await updateState($, bridgeOptions, sessionId, (latest) => ({ ...latest, current }))
      }
      if (epoch !== sessionGeneration || !await ownershipRoutingActive($, hasOwnershipOptions)) {
        clearInactive()
        return next(e)
      }
      const item: PendingPrompt = {
        sessionId,
        queueId: result.requestId,
        requestId: result.requestId,
        prompt: e.text,
        originKind: e.origin.kind,
      }
      addPending(item)
      try {
        const context = [...(e.context ?? []), result.instructions]
        const entered = await next({ ...e, context })
        if (isRecord(entered) && typeof entered.drop === 'string') removePending(item)
        return entered
      } catch (error) {
        removePending(item)
        throw error
      }
    } catch (error) {
      const failure = (error instanceof Error ? error : new Error('native ownership decision failed')) as OwnershipError
      if ('usage_receipt' in failure) {
        $.ui.log(`Smart Router ownership error usage_receipt: ${JSON.stringify(failure.usage_receipt)}`)
      }
      // Malformed protocol and bridge failures stop this prompt; neither is availability fallback.
      throw failure
    }
  })

  on('turn.start', async ($, e, next) => {
    if (!hasOwnershipOptions) return next(e)
    const epoch = sessionGeneration
    const initialization = (async (): Promise<void> => {
      if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
        clearInactive()
        return
      }
      routingActive = true
      const eventOriginKind = turnStartOriginKind(e)
      const item = takePending(e.text, eventOriginKind)
      const originKind = eventOriginKind ?? item?.originKind
      const notificationContinuation = shouldGrantNotificationContinuation(
        originKind,
        item?.originKind,
        hasOpposingRootPrompt(e.text),
      )
      const sessionId = await $.session.id()
      if ((item && sessionId !== item.sessionId) || epoch !== sessionGeneration) {
        throw new Error('stale ownership prompt cannot initialize a different native session')
      }
      if (epoch !== sessionGeneration || !await ownershipRoutingActive($, hasOwnershipOptions)) {
        clearInactive()
        return
      }
      const updated = await updateState($, bridgeOptions, sessionId, (latest) => {
        if (notificationContinuation) {
          if (!latest.current) return null
          const current = { ...latest.current, continuation_turn_id: e.turnId }
          delete current.continuation_prompt_id
          return { ...latest, current }
        }
        if (item?.requestId && latest.current?.request_id === item.requestId) {
          const current = { ...latest.current, turn_id: e.turnId }
          delete current.continuation_turn_id
          delete current.continuation_prompt_id
          return jsonText(current) === jsonText(latest.current)
            ? null
            : { ...latest, current }
        }
        if (item?.requestId) {
          const priorStatus = latest.current?.dispatch_status
          if (!latest.current || ['completed', 'failed'].includes(priorStatus ?? '')) {
            return { ...latest, current: undecidedRecord(item.requestId, e.turnId) }
          }
          if (latest.current.continuation_turn_id || latest.current.continuation_prompt_id) {
            const current = { ...latest.current }
            delete current.continuation_turn_id
            delete current.continuation_prompt_id
            return { ...latest, current }
          }
          return null
        }
        if (!latest.current?.continuation_turn_id && !latest.current?.continuation_prompt_id) return null
        const current = { ...latest.current }
        delete current.continuation_turn_id
        delete current.continuation_prompt_id
        return { ...latest, current }
      })
      if (originKind === 'task-notification') return
      if (item?.requestId &&
        (updated.current?.request_id !== item.requestId || updated.current.turn_id !== e.turnId)) {
        throw new Error('queued ownership prompt cannot replace an active native request')
      }
    })()
    pendingTurnInitializations.set(e.turnId, initialization)
    void initialization.catch(() => undefined)
    await initialization
    if (pendingTurnInitializations.get(e.turnId) === initialization) {
      pendingTurnInitializations.delete(e.turnId)
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (!hasOwnershipOptions) return yield* next(e)
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return yield* next(e)
    }
    routingActive = true
    if (!await waitForTurnInitialization(e.turnId)) {
      throw new Error('ownership turn initialization failed before the native model request')
    }
    if (e.agentId !== undefined) {
      if (e.index === 0) {
        await updateState($, bridgeOptions, await $.session.id(), (state) => {
          if (!state.owners.some((owner) => owner.agent_id === e.agentId)) return null
          const current = state.current
          return {
            ...state,
            owners: state.owners.map((owner) => owner.agent_id === e.agentId
              ? { ...owner, state: 'running' as const } : owner),
            ...(current?.owner_agent_id === e.agentId && current.decision_status === 'ok'
              ? { current: { ...current, dispatch_status: 'started' as const } } : {}),
          }
        })
      }
      return yield* next(e)
    }
    const stream = next(e)
    let refreshed = false
    for await (const chunk of stream) {
      if (!refreshed) {
        refreshed = true
        try {
          const state = await readState($, bridgeOptions, await $.session.id())
          setStatus(state)
          $.ui.invalidate('ui.render')
        } catch (error) {
          $.ui.log(`Smart Router ownership status refresh failed: ${String(error)}`)
        }
      }
      yield chunk
    }
    const result = await stream.result
    if (!refreshed) {
      try {
        setStatus(await readState($, bridgeOptions, await $.session.id()))
        $.ui.invalidate('ui.render')
      } catch (error) {
        $.ui.log(`Smart Router ownership status refresh failed: ${String(error)}`)
      }
    }
    return result
  })

  on('session.end', { sessionId: /./ }, ($, e, next) => {
    clearInactive()
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return next(e)
    }
    routingActive = true
    if (e.agentId !== undefined) return next(e)
    if (!await waitForTurnInitialization()) {
      return deny('Ownership state could not be initialized; no task tool may run for this turn.')
    }
    const sessionId = await $.session.id()
    const state = await readState($, bridgeOptions, sessionId)
    const current = state.current
    if (current?.decision_status === 'undecided' || current?.dispatch_status === 'undecided') {
      return deny('The complete native request has not passed ownership capture; no task tool may run yet.')
    }
    const liveOwners = directOwners(await $.agent.list())
    if (current?.dispatch_status === 'awaiting_dispatch') {
      if (current.dispatch === 'spawn_subagent') {
        const tools = await $.tool.list()
        if ((e.tool !== 'Agent' && e.tool !== 'Task') || !tools.some((tool) => tool.name === e.tool)) {
          return deny('A native Agent or Task dispatch is required before the parent can use other tools.')
        }
        if (!isString(e.tool_use_id)) {
          return deny('The native Agent dispatch has no call identity; resubmit before starting the owner.')
        }
        const claimed = await updateState($, bridgeOptions, sessionId, (latest) =>
          latest.current?.request_id === current.request_id && latest.current.dispatch_status === 'awaiting_dispatch' &&
            latest.current.dispatch === 'spawn_subagent' && latest.current.agent_tool_use_id === undefined
            ? { ...latest, current: { ...latest.current, agent_tool_use_id: e.tool_use_id } }
            : null)
        if (claimed.current?.request_id !== current.request_id || claimed.current.dispatch !== 'spawn_subagent') {
          return deny('The ownership request changed before native dispatch; resubmit the task.')
        }
        const initialDispatch = claimed.current.dispatch_status === 'awaiting_dispatch'
        nativeToolCalls.set(e.tool_use_id, { requestId: current.request_id, initialDispatch })
        try {
          const response = await next(e)
          if (isRecord(response) && (typeof response.deny === 'string' || response.isError === true)) {
            if (initialDispatch) await failInitialDispatchIfIdle($, bridgeOptions, nativeToolCalls, sessionId,
              current.request_id, e.tool_use_id)
            return response
          }
          const after = await readState($, bridgeOptions, sessionId)
          if (initialDispatch && after.current?.request_id === current.request_id &&
            after.current.dispatch_status === 'awaiting_dispatch') {
            await failInitialDispatchIfIdle($, bridgeOptions, nativeToolCalls, sessionId,
              current.request_id, e.tool_use_id)
          }
          return response
        } catch (error) {
          if (initialDispatch) await failInitialDispatchIfIdle($, bridgeOptions, nativeToolCalls, sessionId,
            current.request_id, e.tool_use_id)
          throw error
        } finally {
          nativeToolCalls.delete(e.tool_use_id)
        }
      }
      if (current.dispatch === 'reuse_subagent') {
        const ownerId = current.owner_agent_id
        const persisted = state.owners.find((item) => item.agent_id === ownerId)
        const live = liveOwners.find((agent) => agent.id === ownerId)
        const ownerIsAvailable = Boolean(persisted && persisted.state !== 'failed' &&
          (live ? live.description === persisted.scope : persisted.state === 'completed'))
        if (!ownerId || !ownerIsAvailable) {
          await updateState($, bridgeOptions, sessionId, (latest) => latest.current?.request_id === current.request_id
            ? { ...latest, current: { ...latest.current, dispatch_status: 'failed' } }
            : null)
          return deny('The selected native owner is no longer available; the parent may take over from retained evidence.')
        }
        const allowed = e.tool === 'SendMessage' && e.to === ownerId
        if (!allowed) return deny('Only native coordination with the selected owner is allowed before continuing it.')
        if (!isString(e.tool_use_id)) return deny('The native continuation has no call identity; retry the same owner.')
        const claimed = await updateState($, bridgeOptions, sessionId, (latest) =>
          latest.current?.request_id === current.request_id && latest.current.dispatch_status === 'awaiting_dispatch'
            ? { ...latest, current: { ...latest.current, agent_tool_use_id: e.tool_use_id } }
            : null)
        if (claimed.current?.request_id !== current.request_id ||
          claimed.current.agent_tool_use_id !== e.tool_use_id || claimed.current.dispatch_status !== 'awaiting_dispatch') {
          return deny('The ownership request changed before native continuation; resubmit the task.')
        }
        nativeToolCalls.set(e.tool_use_id, { requestId: current.request_id, initialDispatch: false })
        try {
          const response = await next(e)
          const resumed = successfulNativeOwnerResume(response, ownerId)
          if (isRecord(response) && typeof response.deny === 'string') return response
          if (!resumed) {
            await updateState($, bridgeOptions, sessionId, (latest) =>
              latest.current?.request_id === current.request_id && latest.current.dispatch_status === 'awaiting_dispatch'
                ? { ...latest, current: { ...latest.current, dispatch_status: 'failed' } }
                : null)
            return response
          }
          await updateState($, bridgeOptions, sessionId, (latest) => latest.current?.request_id === current.request_id
            ? {
                ...latest,
                owners: latest.owners.map((item) => item.agent_id === ownerId && item.state !== 'completed'
                  ? { ...item, state: 'running' }
                  : item),
                current: {
                  ...latest.current,
                  dispatch_status: latest.current.dispatch_status === 'awaiting_dispatch'
                    ? 'started'
                    : latest.current.dispatch_status,
                },
              }
            : null)
          return response
        } finally {
          nativeToolCalls.delete(e.tool_use_id)
        }
      }
    }
    if ((e.tool === 'Agent' || e.tool === 'Task') && isString(e.tool_use_id)) {
      nativeToolCalls.set(e.tool_use_id, { requestId: current?.request_id ?? null, initialDispatch: false })
      try {
        return await next(e)
      } finally {
        nativeToolCalls.delete(e.tool_use_id)
      }
    }
    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    if (!hasOwnershipOptions) return next(e)
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return next(e)
    }
    routingActive = true
    if (e.parentAgentId !== undefined || e.provider.plugin !== 'engine') return next(e)
    const call = nativeToolCalls.get(e.tool_use_id)
    if (!call) return next(e)
    const sessionId = await $.session.id()
    const state = await readState($, bridgeOptions, sessionId)
    const current = state.current
    const isSelectedDispatch = Boolean(call.initialDispatch && call.requestId && current?.request_id === call.requestId &&
      current.dispatch === 'spawn_subagent' && current.dispatch_status === 'awaiting_dispatch')
    const response = await next(e)
    if (isRecord(response) && (typeof response.deny === 'string' || response.isError === true)) return response
    const agentId = isRecord(response) ? response.agentId : undefined
    if (!isString(agentId)) return response
    await updateState($, bridgeOptions, sessionId, (latest) => {
      const owners = latest.owners.filter((owner) => owner.agent_id !== agentId)
      owners.push({ agent_id: agentId, scope: e.description, state: 'running' })
      const selected = isSelectedDispatch && latest.current?.request_id === call.requestId &&
        latest.current.dispatch_status === 'awaiting_dispatch'
      const tracksOrdinarySpawn = !selected && call.requestId !== null &&
        latest.current?.request_id === call.requestId && latest.current.ownership === 'main' &&
        latest.current.dispatch === 'work_directly'
      return {
        ...latest,
        owners,
        ...(selected && latest.current
          ? { current: { ...latest.current, agent_tool_use_id: e.tool_use_id, owner_agent_id: agentId, dispatch_status: 'started' } }
          : tracksOrdinarySpawn && latest.current
            ? { current: { ...latest.current, owner_agent_id: agentId } }
          : {}),
      }
    })
    $.ui.invalidate('ui.render')
    return response
  })

  on('turn.complete', { isAborted: [true, false] }, async ($, e, next) => {
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return next(e)
    }
    routingActive = true
    const sessionId = await $.session.id()
    const current = (await readState($, bridgeOptions, sessionId)).current
    if (!current && e.agentId === undefined) {
      setStatus(null)
      return next(e)
    }
    if (e.agentId !== undefined) {
      const owner = directOwners(await $.agent.list()).find((agent) => agent.id === e.agentId)
      const status = e.reason === 'answer' ? 'completed' : 'failed'
      await updateState($, bridgeOptions, sessionId, (latest) => {
        const owners = latest.owners.map((item) => item.agent_id === e.agentId
          ? { ...item, state: status }
          : item)
        if (!owners.some((item) => item.agent_id === e.agentId) && owner) {
          owners.push({ agent_id: owner.id, scope: owner.description, state: status })
        }
        const active = latest.current?.request_id === current?.request_id &&
          latest.current.owner_agent_id === e.agentId && latest.current.dispatch_status === 'started'
        const ordinaryOwner = latest.current?.request_id === current?.request_id &&
          latest.current.owner_agent_id === e.agentId && latest.current.ownership === 'main' &&
          latest.current.dispatch === 'work_directly'
        const nextRunningOwner = owners.find((item) => item.state === 'running')
        const currentAfterOwnerCompletion = ordinaryOwner && latest.current
          ? { ...latest.current, ...(nextRunningOwner ? { owner_agent_id: nextRunningOwner.agent_id } : {}) }
          : active && latest.current
            ? nextRunningOwner
              ? { ...latest.current, owner_agent_id: nextRunningOwner.agent_id, dispatch_status: 'started' as const }
              : { ...latest.current, dispatch_status: status === 'completed' ? 'completed' as const : 'failed' as const }
            : undefined
        return {
          ...latest,
          owners,
          ...(currentAfterOwnerCompletion ? { current: currentAfterOwnerCompletion } : {}),
        }
      })
    } else if (current.turn_id === e.turnId && current.ownership === 'main') {
      await updateState($, bridgeOptions, sessionId, (latest) => latest.current?.request_id === current.request_id
        ? { ...latest, current: { ...latest.current, dispatch_status: e.reason === 'answer' ? 'completed' : 'failed' } }
        : null)
    } else if (current.turn_id === e.turnId && current.dispatch_status === 'awaiting_dispatch') {
      await updateState($, bridgeOptions, sessionId, (latest) => latest.current?.request_id === current.request_id
        ? { ...latest, current: { ...latest.current, dispatch_status: 'failed' } }
        : null)
    }
    $.ui.invalidate('ui.render')
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    if (!await ownershipRoutingActive($, hasOwnershipOptions)) {
      clearInactive()
      return next(e)
    }
    routingActive = true
    const sessionId = await $.session.id()
    const current = (await readState($, bridgeOptions, sessionId)).current
    if (!current) return next(e)
    if (e.stop_hook_active && answerFeedbackRequestId === current.request_id) return next(e)
    if (!e.stop_hook_active && (current.decision_status === 'undecided' || current.dispatch_status === 'undecided')) {
      return { block: 'The native request is still awaiting ownership capture; do not accept a final answer yet.' }
    }
    if (!e.stop_hook_active && current.decision_status === 'ok' &&
      ['awaiting_dispatch', 'started'].includes(current.dispatch_status) &&
      ['spawn_subagent', 'reuse_subagent'].includes(current.dispatch ?? '')) {
      return { block: 'The selected native owner has not completed this task; continue coordination or report a concrete dispatch failure.' +
        (answerDelivery === true ? ' This answer has not been delivered. The next final answer must contain the complete deliverable or its selection.' : '') }
    }
    if (answerDelivery === true && typeof e.last_assistant_message === 'string') {
      const result = await runBridge($, bridgeOptions, {
        operation: 'validate_answer', session_id: sessionId,
        request: { final_text: e.last_assistant_message },
      }, 10000)
      if (!hasExactKeys(result, ['error']) || (result.error !== null && !isString(result.error))) {
        throw new Error('ownership answer validation response has an invalid shape')
      }
      if (result.error !== null) {
        answerFeedbackRequestId = current.request_id
        return { block: `The final answer selection is invalid: ${result.error}. Return a complete normal answer or a corrected selection of completed owned children. The next final message replaces the previous answer.` }
      }
    }
    return next(e)
  })

}
