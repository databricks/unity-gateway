export type TreeStatus = 'pending' | 'running' | 'waiting' | 'idle' | 'completed' | 'failed' | 'killed'
export type RouteStatus = 'selecting' | 'selected' | 'fallback' | 'denied'

export interface TreeNode {
  id: string
  parentId?: string
  name: string
  role: string
  model?: string
  usageModel?: string
  effort?: string
  task: string
  status: TreeStatus
  activity?: string
  activityAt?: number
  lastTool?: string
  background?: boolean
  resumed?: boolean
  isTeammate?: boolean
  teammateId?: string
  startedAt?: number
  endedAt?: number
  inputTokens?: number
  outputTokens?: number
  toolCount: number
  errorCount: number
  assistantText?: string
  lastOutcome?: 'running' | 'ok' | 'failed'
}

export interface TreeRoute {
  toolId: string
  name: string
  task: string
  parentId?: string
  status: RouteStatus
  model?: string
  rationale?: string
  seconds?: number
  agentId?: string
  /** Set when a later launch of the same task succeeded: the denial is folded
   * into that child instead of rendering as its own subagent. */
  supersededBy?: string
}

export interface TreeEvent { at: number; agentId: string; action: string; detail?: string; streamKey?: string }

type AgentSnapshot = {
  id: string; status: TreeStatus; type: string; description: string
  parentId?: string; name?: string; teammateId?: string
}
type SpawnInput = {
  tool_use_id: string; description: string; subagentType: string; background: boolean
  parentAgentId?: string; model?: string; isTeammate?: true; name?: string
  resume?: { agentId: string }
}
type SpawnResult = { model?: string; agentId?: string; teammateId?: string; deny?: string }
type TurnInput = {
  turnId: string; agentId?: string; answer?: string; reason?: 'answer' | 'aborted' | 'refusal' | 'error'
  usage?: Usage | null; isAborted?: boolean
}
type StepInput = { turnId: string; index: number; model: string; agentId?: string; effort?: string | number }
type StepResult = { turnId: string; index: number; answer?: string; stopReason: string | null; usage: Usage | null }
type ToolInput = { tool: string; tool_use_id?: string; agentId?: string; file_path?: unknown; notebook_path?: unknown }
type Usage = {
  model?: string; input_tokens?: number; output_tokens?: number
  cache_read_input_tokens?: number; cache_creation_input_tokens?: number
}
type Totals = { inputTokens: number; outputTokens: number; model?: string }
type TurnUsage = { agentId: string; steps: Map<number, Totals>; total?: Totals }
type PendingSpawn = {
  at: number; parentId?: string; name: string; task: string; role: string
  background: boolean; resumeId?: string; teammate: boolean
}
type Lifecycle = { activeAt?: number; endedAt?: number; deniedAt?: number }
type ToolRecord = { agentId: string; activity: string; finished: boolean; errorCounted: boolean }

const MAIN_ID = 'main'
const MAX_EVENTS = 200
const MAX_ASSISTANT_TEXT = 32768
const terminal = new Set<TreeStatus>(['completed', 'failed', 'killed'])
const ansi = /\u001B(?:\[[0-?]*[ -/]*[@-~]|\][^\u0007]*(?:\u0007|\u001B\\))/g

export function safeText(value: unknown, maxLength = 120): string {
  if (typeof value !== 'string') return ''
  const limit = Math.max(1, Math.min(32768, Math.floor(maxLength)))
  const text = value.replace(ansi, '').replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').replace(/\s+/g, ' ').trim()
  const chars = Array.from(text)
  return chars.length > limit ? chars.slice(0, limit - 1).join('') + '…' : text
}

function pathBasename(value: string): string {
  const clean = value.replace(/[.,;:!?)}\]]+$/g, '')
  return clean.split(/[\\/]/).filter(Boolean).pop() || 'file'
}

const SHELL_COMMANDS = new Set([
  'cat', 'ls', 'cd', 'pwd', 'grep', 'rg', 'find', 'sed', 'awk', 'git', 'curl', 'wget',
  'python', 'python3', 'bash', 'sh', 'npm', 'uv', 'node', 'echo', 'printf', 'rm', 'mv', 'cp', 'chmod', 'chown', 'sudo',
  'docker', 'kubectl', 'pip', 'pnpm', 'yarn', 'make', 'ssh', 'scp', 'tar', 'ps', 'kill', 'env', 'export', 'exec',
])
const CODE_STARTS = ['const ', 'let ', 'function ', 'import ', 'export ', 'def ', 'class ', 'return ', 'if (', 'for (']

function isShellCommand(value: string): boolean {
  const first = value.trim().split(' ')[0]?.toLowerCase() ?? ''
  return SHELL_COMMANDS.has(first)
}

function isShellLine(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  if (/^(?:\$|❯)\s|^PS(?:\s|>)/.test(trimmed) || /^[A-Za-z_][A-Za-z0-9_]*=\S/.test(trimmed)) return true
  return isShellCommand(trimmed)
}

function omitCode(value: string): string {
  let result = ''
  let cursor = 0
  while (cursor < value.length) {
    const start = value.indexOf('`', cursor)
    if (start < 0) return result + value.slice(cursor)
    const fenced = value.startsWith('```', start)
    const marker = fenced ? '```' : '`'
    const end = value.indexOf(marker, start + marker.length)
    result += value.slice(cursor, start)
    if (end < 0) return result + ' [code omitted]'
    const code = value.slice(start + marker.length, end).trim()
    // A credential assignment must be omitted whole, never basename-shortened.
    const credentialCode = isCredentialName(code.split(/[=:]/)[0])
    const pathLike = !fenced && !isShellCommand(code) && !credentialCode && /^[^|;&<>$]*[\\/][^\\/\s]+$/.test(code)
    result += pathLike ? pathBasename(code) : fenced || isShellCommand(code) || code.includes('/') || code.includes('\\')
      ? ' [code omitted]' : code
    cursor = end + marker.length
  }
  return result
}

function isCredentialName(name: string): boolean {
  return /(?:^|[_-])(?:token|key|secret|password|authorization|credential)(?:$|[_-])/i.test(name)
    || /[a-z0-9](?:Token|Key|Secret|Password|Authorization|Credential)(?:$|[_-])/i.test(name)
}

function colonCredentialKey(word: string): boolean {
  if (!word.includes(':')) return false
  const key = word.replace(/^["'{\s]+/, '').replace(/["':\s]+$/, '')
  return key.length > 0 && key !== word && isCredentialName(key)
}

function safeToken(value: string): string {
  let start = 0
  let end = value.length
  while (start < end && `"'([{<`.includes(value[start])) start += 1
  while (end > start && `.,;:!?)]}>"'`.includes(value[end - 1])) end -= 1
  const prefix = value.slice(0, start)
  const suffix = value.slice(end)
  const token = value.slice(start, end)
  const equal = token.indexOf('=')
  if (equal > 0 && isCredentialName(token.slice(0, equal))) {
    return prefix + token.slice(0, equal + 1) + '[redacted]' + suffix
  }
  // JSON without spaces keeps `key:value` in one word, quotes included.
  const colon = token.indexOf(':')
  if (colon > 0 && colon < token.length - 1 && !/^[a-z][a-z0-9+.-]*:\/\//i.test(token)
      && isCredentialName(token.slice(0, colon).replace(/["']/g, ''))) {
    return prefix + token.slice(0, colon + 1) + '[redacted]' + suffix
  }
  // Query strings carry credentials in otherwise ordinary URLs.
  const query = token.indexOf('?')
  if (query >= 0) {
    const params = token.slice(query + 1)
    const redacted = params.replace(/([&]?)([^&=]+)=([^&]*)/g, (all, amp, name, value) =>
      value && isCredentialName(name) ? `${amp}${name}=[redacted]` : all)
    if (redacted !== params) {
      return prefix + token.slice(0, query + 1) + redacted + suffix
    }
  }
  const path = equal > 0 ? token.slice(equal + 1) : token
  const assignment = equal > 0 ? token.slice(0, equal + 1) : ''
  if (path.toLowerCase().startsWith('file://')) return prefix + assignment + pathBasename(path.slice(7)) + suffix
  const windowsPath = path.length > 2 && /[a-z]/i.test(path[0]) && path[1] === ':' &&
    (path.charCodeAt(2) === 47 || path.charCodeAt(2) === 92)
  if (path.startsWith('/') || path.startsWith('~/') || path.startsWith('\\\\') || windowsPath) {
    return prefix + assignment + pathBasename(path) + suffix
  }
  // A relative path (spaces make the earlier segments ordinary words) still
  // leaks directory structure: reduce any slashed token whose last segment
  // names a file to that basename. URLs and plain prose like "and/or" stay.
  if (path.includes('/') && !/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) {
    const last = path.slice(path.lastIndexOf('/') + 1)
    if (/\.[a-z0-9]{1,8}$/i.test(last) && last.length > 1) {
      return prefix + assignment + last + suffix
    }
  }
  if (token.length > 128 && !/\s/.test(token)) return prefix + '[long value omitted]' + suffix
  return value
}

export function safePreviewText(value: unknown, maxLength = 240): string {
  if (typeof value !== 'string') return ''
  const limit = Math.max(1, Math.min(MAX_ASSISTANT_TEXT, Math.floor(maxLength)))
  const clean = value.replace(ansi, '').replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, ' ')
  const lines = omitCode(clean).split('\n')
  let redactNext = false
  const safeLines = lines.map(line => {
    const trimmed = line.trim()
    if (!trimmed) return ''
    if (isShellLine(trimmed) || CODE_STARTS.some(start => trimmed.startsWith(start))) return undefined
    const words = trimmed.split(/\s+/)
    return words.map(word => {
      const token = safeToken(word)
      // A marker word is shown, not consumed: `Authorization: Bearer <token>`
      // must still redact the token after Bearer.
      const marker = ['bearer', 'basic'].includes(token.toLowerCase()) || colonCredentialKey(word)
      if (redactNext && !marker) { redactNext = false; return '[redacted]' }
      if (marker) redactNext = true
      return token
    }).join(' ')
  }).filter((line): line is string => line !== undefined)
  const safe = safeLines.join('\n').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim()
  const chars = Array.from(safe)
  if (chars.length <= limit) return safe || (value.trim() ? 'Code excerpt omitted' : '')

  const prefix = chars.slice(0, limit).join('')
  const sentenceEnds = [...prefix.matchAll(/[.!?](?=\s|$)/g)]
  const sentenceEnd = sentenceEnds.at(-1)?.index
  const newlineEnd = prefix.lastIndexOf('\n')
  const boundary = Math.max(sentenceEnd === undefined ? 0 : sentenceEnd + 1, newlineEnd)
  if (boundary > 0) return prefix.slice(0, boundary).trimEnd()
  const wordEnd = prefix.lastIndexOf(' ')
  return (wordEnd > 0 ? prefix.slice(0, wordEnd) : prefix).trimEnd()
}

export function duration(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

export function activityMark(node: TreeNode, now: number): string {
  if (node.status === 'running') return ['⠋', '⠙', '⠹', '⠸'][Math.floor(now / 500) % 4]
  return { pending: '○', waiting: '◌', idle: '○', completed: '✓', failed: '×', killed: '■' }[node.status]
}

export function activityText(node: TreeNode): string {
  if (node.status === 'waiting') return 'Waiting'
  if (node.status === 'pending') return 'Starting'
  return node.activity || (node.status === 'running' ? 'Working' : node.status)
}

function toolActivity(e: ToolInput): string {
  const action = { Read: 'Reading', Edit: 'Editing', Write: 'Writing', NotebookEdit: 'Editing notebook',
    Bash: 'Running command', Grep: 'Searching content', Glob: 'Finding files', WebFetch: 'Reading web page',
    WebSearch: 'Searching web', Agent: 'Spawning subagent', Task: 'Spawning subagent',
    TaskOutput: 'Waiting for result', SubagentHandback: 'Returning answer' }[e.tool]
  const path = e.tool === 'NotebookEdit' ? e.notebook_path :
    ['Read', 'Edit', 'Write'].includes(e.tool) ? e.file_path : undefined
  const name = typeof path === 'string' ? safeText(path.split(/[\\/]/).pop(), 80) : ''
  return (action || `Using ${safeText(e.tool, 60)}`) + (name ? ` ${name}` : '')
}

const nodes = new Map<string, TreeNode>()
const events: TreeEvent[] = []
const routes = new Map<string, TreeRoute>()
const pendingSpawns = new Map<string, PendingSpawn>()
const lifecycle = new Map<string, Lifecycle>()
const activeTurns = new Map<string, string>()
const startedSteps = new Set<string>()
const turnUsage = new Map<string, TurnUsage>()
const failedTurns = new Set<string>()
const tools = new Map<string, ToolRecord>()
const assistantBuffers = new Map<string, { step: string; text: string; capped?: boolean }>()
let main = makeMain()
// Set by tree-relevant child activity and consumed by the registration's
// invalidation gates, so main-loop steps and turns do not redraw the session.
let renderDirty = false

function markRenderDirty(agentId: string | undefined): void {
  if (agentId !== undefined && agentId !== MAIN_ID) renderDirty = true
}

function makeMain(): TreeNode {
  return { id: MAIN_ID, name: 'Main', role: 'main', task: 'Main session', status: 'idle', toolCount: 0, errorCount: 0 }
}

function nodeFor(id?: string): TreeNode {
  if (!id || id === MAIN_ID) return main
  let node = nodes.get(id)
  if (!node) {
    node = { id, name: '', role: '', task: '', status: 'pending', toolCount: 0, errorCount: 0 }
    nodes.set(id, node)
  }
  return node
}

function lifeFor(id: string): Lifecycle {
  let life = lifecycle.get(id)
  if (!life) { life = {}; lifecycle.set(id, life) }
  return life
}

function activeTool(id: string): string | undefined {
  let activity: string | undefined
  for (const tool of tools.values()) {
    if (tool.agentId === id && !tool.finished) activity = tool.activity
  }
  return activity
}

function emit(at: number, agentId: string, action: string, detail?: string, streamKey?: string): void {
  const node = nodeFor(agentId)
  node.activityAt = Math.max(node.activityAt ?? -Infinity, at)
  if (agentId === MAIN_ID) return
  events.push({ at, agentId, action, ...(detail ? { detail } : {}), ...(streamKey ? { streamKey } : {}) })
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS)
}

function emitMessage(at: number, agentId: string, streamKey: string, detail: string): void {
  const node = nodeFor(agentId)
  node.activityAt = Math.max(node.activityAt ?? -Infinity, at)
  const existing = events.find(event => event.streamKey === streamKey)
  if (existing) { existing.at = at; existing.detail = detail; return }
  emit(at, agentId, 'assistant message', detail, streamKey)
}

function endedChild(node: TreeNode): boolean {
  return node.id !== MAIN_ID && (node.endedAt !== undefined || node.status === 'idle' || terminal.has(node.status))
}

function staleTurn(node: TreeNode, turnId: string): boolean {
  const active = activeTurns.get(node.id)
  return active !== undefined && active !== turnId
}

function markActive(node: TreeNode, at: number): void {
  const life = lifeFor(node.id)
  life.activeAt = at
  if (node.endedAt !== undefined) { node.startedAt = at; node.endedAt = undefined }
  else node.startedAt ??= at
  node.status = 'running'
}

function markEnded(node: TreeNode, status: TreeStatus, at: number, activity: string): void {
  const life = lifeFor(node.id)
  if (node.endedAt === undefined || (life.activeAt ?? -Infinity) > (life.endedAt ?? -Infinity)) {
    node.endedAt = at
    life.endedAt = at
  }
  node.status = status
  node.activity = activity
}

function turnKey(agentId: string, turnId: string): string {
  return agentId.length + ':' + agentId + turnId
}

function usageFrom(usage: Usage | null | undefined): Totals | undefined {
  if (!usage) return undefined
  if (usage.input_tokens === undefined && usage.output_tokens === undefined &&
      usage.cache_read_input_tokens === undefined && usage.cache_creation_input_tokens === undefined) return undefined
  return {
    inputTokens: (usage.input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0) +
      (usage.cache_creation_input_tokens ?? 0),
    outputTokens: usage.output_tokens ?? 0,
    model: usage.model,
  }
}

function recordUsage(agentId: string, turnId: string, index: number, usage: Totals, total = false): void {
  const key = turnKey(agentId, turnId)
  let turn = turnUsage.get(key)
  if (!turn) { turn = { agentId, steps: new Map() }; turnUsage.set(key, turn) }
  if (total) turn.total = usage
  else if (!turn.total) turn.steps.set(index, usage)

  let inputTokens = 0
  let outputTokens = 0
  let found = false
  for (const record of turnUsage.values()) {
    if (record.agentId !== agentId) continue
    for (const value of record.total ? [record.total] : record.steps.values()) {
      inputTokens += value.inputTokens
      outputTokens += value.outputTokens
      found = true
    }
  }
  if (found) {
    const node = nodeFor(agentId)
    node.inputTokens = inputTokens
    node.outputTokens = outputTokens
  }
}

function applySpawn(node: TreeNode, spawn: PendingSpawn): void {
  if (!spawn.resumeId) node.parentId = spawn.parentId ?? MAIN_ID
  node.name = spawn.name || node.name
  node.role = spawn.role || node.role
  node.task = spawn.task || node.task
  node.background = spawn.background
  if (spawn.teammate) node.isTeammate = true
}

export const agentTree = {
  get main(): TreeNode { return main },

  // True once, when tree-relevant child activity happened since the last read.
  consumeRenderDirty(): boolean { const was = renderDirty; renderDirty = false; return was },
  nodes,
  events,
  routes,

  routeStarted(toolId: string, name: string, task: string, parentId: string | undefined, now: number): void {
    renderDirty = true
    routes.set(toolId, { toolId, name, task, parentId, status: 'selecting' })
    emit(now, parentId ?? MAIN_ID, 'route selecting', name)
  },

  routeFinished(toolId: string, route: { model: string; rationale: string; seconds?: number } | undefined, now: number): void {
    renderDirty = true
    const current = routes.get(toolId)
    if (!current) return
    if (route) {
      current.status = 'selected'
      current.model = route.model
      current.rationale = route.rationale
      if (route.seconds !== undefined) current.seconds = route.seconds
    } else current.status = 'fallback'
    emit(now, current.parentId ?? MAIN_ID, route ? 'route selected' : 'route fallback', current.model ?? current.name)
  },

  spawnStarted(e: SpawnInput, now: number): void {
    markRenderDirty(e.parentAgentId)
    let route = routes.get(e.tool_use_id)
    if (!route) {
      route = { toolId: e.tool_use_id, name: e.name ?? e.subagentType, task: e.description,
        parentId: e.parentAgentId, status: 'fallback' }
      routes.set(e.tool_use_id, route)
    }
    const spawn: PendingSpawn = {
      at: now, parentId: e.parentAgentId ?? route.parentId, name: e.name ?? route.name,
      task: e.description || route.task, role: e.subagentType, background: e.background,
      resumeId: e.resume?.agentId, teammate: e.isTeammate === true,
    }
    pendingSpawns.set(e.tool_use_id, spawn)
    if (spawn.resumeId) applySpawn(nodeFor(spawn.resumeId), spawn)
    emit(now, spawn.parentId ?? MAIN_ID, 'spawn requested', spawn.name)
  },

  spawnFinished(e: SpawnInput, result: SpawnResult, now: number): void {
    markRenderDirty(e.parentAgentId)
    const id = e.tool_use_id
    const spawn = id === undefined ? undefined : pendingSpawns.get(id)
    const route = id === undefined ? undefined : routes.get(id)
    const parentId = spawn?.parentId ?? e.parentAgentId ?? route?.parentId ?? MAIN_ID
    if (result.deny !== undefined) {
      if (route) {
        route.status = 'denied'
        lifeFor(route.toolId).deniedAt = now
      }
      const tool = tools.get(id)
      if (!tool?.errorCounted) {
        nodeFor(parentId).errorCount += 1
        if (tool) tool.errorCounted = true
      }
      nodeFor(parentId).activity = 'Spawn denied'
      emit(now, parentId, 'spawn denied', route?.name)
      // A denied resume request leaves only a placeholder target behind; an
      // empty roster would never retire it.
      if (spawn?.resumeId) {
        const target = nodes.get(spawn.resumeId)
        if (target && target.status === 'pending' && target.toolCount === 0) nodes.delete(spawn.resumeId)
      }
      if (id !== undefined) pendingSpawns.delete(id)
      return
    }

    const agentId = result.agentId ?? e.resume?.agentId
    if (agentId !== undefined) {
      // A denied attempt of the same task from the same parent within a short
      // window is the router's relaunch flow, not a separate subagent: fold it
      // into the child that actually ran. Unsuperseded denials still render.
      for (const other of routes.values()) {
        if (other.status === 'denied' && !other.supersededBy && other.toolId !== id
            && (other.parentId ?? MAIN_ID) === parentId
            && other.task === (spawn?.task ?? e.description)
            && now - (lifeFor(other.toolId).deniedAt ?? -Infinity) <= 60_000) {
          other.supersededBy = agentId
        }
      }
    }
    if (route) {
      route.agentId = agentId
      if (route.status === 'selecting') route.status = 'fallback'
    }
    if (agentId) {
      const node = nodeFor(agentId)
      const info = spawn ?? {
        at: now, parentId: e.parentAgentId ?? route?.parentId, name: e.name ?? route?.name ?? e.subagentType,
        task: e.description, role: e.subagentType, background: e.background,
        resumeId: e.resume?.agentId, teammate: e.isTeammate === true,
      }
      applySpawn(node, info)
      if (info.resumeId) node.resumed = true
      if (result.teammateId) { node.teammateId = result.teammateId; node.isTeammate = true }
      if (result.model !== undefined) node.model = result.model

      const life = lifeFor(agentId)
      if (node.endedAt === undefined || info.at > (life.endedAt ?? -Infinity)) {
        if (node.endedAt !== undefined) { node.startedAt = info.at; node.endedAt = undefined }
        else node.startedAt ??= info.at
        node.status = 'running'
        node.activity = activeTool(node.id) ?? 'Working'
        life.activeAt = Math.max(life.activeAt ?? -Infinity, info.at)
      }
    }
    emit(now, agentId ?? parentId, agentId ? 'agent started' : 'spawn resolved', route?.name ?? result.model)
    if (id !== undefined) pendingSpawns.delete(id)
  },

  stepStarted(e: StepInput, now: number): void {
    markRenderDirty(e.agentId)
    const node = nodeFor(e.agentId)
    if (endedChild(node) || staleTurn(node, e.turnId)) return
    const key = turnKey(node.id, e.turnId) + ':' + e.index
    if (startedSteps.has(key)) return
    startedSteps.add(key)
    markActive(node, now)
    if (node.id === MAIN_ID) node.model = e.model
    if (e.effort !== undefined) node.effort = String(e.effort)
    node.activity = 'Working'
    emit(now, node.id, 'step started', 'step ' + (e.index + 1))
  },

  assistantTextChunk(agentId: string, turnId: string, index: number, text: string, now?: number): void {
    markRenderDirty(agentId)
    const node = nodeFor(agentId)
    if (endedChild(node) || staleTurn(node, turnId)) return
    const step = turnKey(node.id, turnId) + ':' + index
    let buffer = assistantBuffers.get(node.id)
    if (!buffer || buffer.step !== step) { buffer = { step, text: '', capped: false }; assistantBuffers.set(node.id, buffer) }
    // Once the preview is full, later chunks cannot change the retained head:
    // stop appending, sanitizing, and re-emitting the same text.
    if (buffer.capped) return
    // The capture cap only needs the full code-point pass once the buffer is
    // full; growing buffers append directly.
    buffer.text = buffer.text.length + text.length <= MAX_ASSISTANT_TEXT + 1
      ? buffer.text + text
      : Array.from(buffer.text + text).slice(0, MAX_ASSISTANT_TEXT + 1).join('')
    if (buffer.text.length > MAX_ASSISTANT_TEXT) buffer.capped = true
    node.assistantText = safePreviewText(buffer.text, MAX_ASSISTANT_TEXT)
    emitMessage(now ?? Date.now(), node.id, step, node.assistantText)
  },

  stepFinished(e: StepInput, result: StepResult, now: number): void {
    markRenderDirty(e.agentId)
    const node = nodeFor(e.agentId)
    const isLate = endedChild(node) || staleTurn(node, result.turnId)
    const usage = usageFrom(result.usage)
    if (usage) {
      if (!isLate && usage.model !== undefined) node.usageModel = usage.model
      recordUsage(node.id, result.turnId, result.index, usage)
    }
    if (isLate) return
    assistantBuffers.delete(node.id)
    if (node.id !== MAIN_ID && result.answer) {
      const answer = safePreviewText(result.answer, 32768)
      if (answer) {
        node.assistantText = answer
        emitMessage(now, node.id, turnKey(node.id, result.turnId) + ':' + result.index, answer)
      }
    }
    if (node.endedAt === undefined) node.activity = activeTool(node.id) ?? (result.stopReason === 'tool_use' ? 'Running tools' : 'Working')
    emit(now, node.id, 'step finished', result.stopReason ?? undefined)
  },

  stepFailed(e: StepInput, now: number): void {
    markRenderDirty(e.agentId)
    const node = nodeFor(e.agentId)
    if (endedChild(node) || staleTurn(node, e.turnId)) return
    assistantBuffers.delete(node.id)
    if (node.endedAt === undefined) node.activity = 'Step failed'
    node.lastOutcome = 'failed'
    emit(now, node.id, 'step failed')
  },

  turnStarted(e: TurnInput, now: number): void {
    markRenderDirty(e.agentId)
    const node = nodeFor(e.agentId)
    markActive(node, now)
    activeTurns.set(node.id, e.turnId)
    node.activity = 'Working'
    emit(now, node.id, 'turn started')
  },

  turnFinished(e: TurnInput, now: number): void {
    markRenderDirty(e.agentId)
    const node = nodeFor(e.agentId)
    const isCurrentTurn = activeTurns.get(node.id) === e.turnId
    const isLate = staleTurn(node, e.turnId) || (endedChild(node) && !isCurrentTurn)
    const usage = usageFrom(e.usage)
    if (usage) {
      if (!isLate && usage.model !== undefined) node.usageModel = usage.model
      recordUsage(node.id, e.turnId, -1, usage, true)
    }
    if (isLate) return
    assistantBuffers.delete(node.id)

    const finalAnswer = node.id === MAIN_ID ? '' : safePreviewText(e.answer, 32768)
    if (finalAnswer) {
      node.assistantText = finalAnswer
      const priorMessage = [...events].reverse().find(event =>
        event.agentId === node.id && event.action === 'assistant message')
      if (priorMessage?.detail === finalAnswer) {
        priorMessage.action = 'final answer'
        priorMessage.at = now
      } else emit(now, node.id, 'final answer', finalAnswer)
    }
    const reason = e.reason ?? (e.isAborted ? 'aborted' : 'answer')
    const failed = reason === 'error' || reason === 'refusal'
    const activity = reason === 'refusal' ? 'Refused' : failed ? 'Failed' :
      reason === 'aborted' ? 'Stopped' : 'Completed'
    node.lastOutcome = failed ? 'failed' : 'ok'
    if (node.id === MAIN_ID) markEnded(node, failed ? 'failed' : 'idle', now, failed ? activity : 'Idle')
    else if (node.isTeammate && !failed) markEnded(node, 'idle', now, 'Idle')
    else markEnded(node, failed ? 'failed' : reason === 'aborted' ? 'killed' : 'completed', now, activity)
    activeTurns.delete(node.id)

    if (failed) {
      const key = turnKey(node.id, e.turnId)
      if (!failedTurns.has(key)) { failedTurns.add(key); node.errorCount += 1 }
    }
    emit(now, node.id, failed ? 'turn failed' : reason === 'aborted' ? 'turn stopped' : 'turn completed', reason)
  },

  toolStarted(e: ToolInput, now: number): void {
    markRenderDirty(e.agentId)
    // Invariant: a SubagentHandback is a finished child's delivery receipt, so
    // an agentId with no roster node must never create a tile.
    if (e.tool === 'SubagentHandback' && e.agentId !== undefined && !nodes.has(e.agentId)) return
    const node = nodeFor(e.agentId)
    if (e.tool_use_id !== undefined && tools.has(e.tool_use_id)) return
    if (endedChild(node)) return
    markActive(node, now)
    node.lastOutcome = 'running'
    const activity = toolActivity(e)
    if (e.tool_use_id !== undefined) tools.set(e.tool_use_id, { agentId: node.id, activity, finished: false, errorCounted: false })
    node.toolCount += 1
    node.activity = activity
    emit(now, node.id, 'tool started', activity)
  },

  toolFinished(e: ToolInput, result: { isError?: boolean; deny?: string }, now: number): void {
    markRenderDirty(e.agentId)
    if (e.tool === 'SubagentHandback' && e.agentId !== undefined && !nodes.has(e.agentId)) return
    const node = nodeFor(e.agentId)
    let record = e.tool_use_id === undefined ? undefined : tools.get(e.tool_use_id)
    if (record?.finished) return
    if (!record && e.tool_use_id !== undefined) {
      record = { agentId: node.id, activity: toolActivity(e), finished: false, errorCounted: false }
      tools.set(e.tool_use_id, record)
      node.toolCount += 1
    }
    if (record) record.finished = true
    const owner = record ? nodeFor(record.agentId) : node
    // A handback failure is a delivery problem, never the child's work failing.
    if (e.tool === 'SubagentHandback') {
      owner.lastTool = record?.activity ?? toolActivity(e)
      if (owner.endedAt === undefined) owner.activity = activeTool(owner.id) ?? 'Returning answer'
      emit(now, owner.id, 'tool finished', 'Returning answer')
      return
    }
    const failed = result.isError === true || result.deny !== undefined
    const route = e.tool_use_id ? routes.get(e.tool_use_id) : undefined
    if (failed && route && !route.agentId) {
      route.status = 'denied'
      lifeFor(e.tool_use_id).deniedAt = now
    }
    owner.lastOutcome = failed ? 'failed' : 'ok'
    if (failed && (!record || !record.errorCounted)) {
      owner.errorCount += 1
      if (record) record.errorCounted = true
    }
    owner.lastTool = (failed ? 'Failed: ' : '') + (record?.activity ?? toolActivity(e))
    if (owner.endedAt === undefined) owner.activity = activeTool(owner.id) ?? (failed ? 'Tool failed: ' + safeText(e.tool) : 'Working')
    emit(now, owner.id, failed ? 'tool failed' : 'tool finished', record?.activity ?? toolActivity(e))
  },

  syncAgents(list: readonly AgentSnapshot[], now: number): void {
    for (const agent of list) {
      if (agent.id === MAIN_ID) continue
      const node = nodeFor(agent.id)
      const life = lifeFor(agent.id)
      // A roster snapshot without parentId must not flatten a child whose spawn
      // or route events already captured a nested parent.
      if (agent.parentId !== undefined) node.parentId = agent.parentId
      else node.parentId ??= MAIN_ID
      node.name = node.name || agent.name || agent.type
      node.role = agent.type
      node.task = node.task || agent.description
      if (agent.teammateId) { node.teammateId = agent.teammateId; node.isTeammate = true }

      if (now < Math.max(life.activeAt ?? -Infinity, life.endedAt ?? -Infinity, node.activityAt ?? -Infinity)) continue

      if (terminal.has(node.status) && !terminal.has(agent.status) &&
          (life.activeAt === undefined || life.activeAt <= (life.endedAt ?? -Infinity))) continue
      if (node.status === 'running' && terminal.has(agent.status) &&
          life.activeAt !== undefined && life.endedAt !== undefined && life.activeAt > life.endedAt) continue

      if (node.status !== agent.status) {
        if (agent.status === 'running') node.activity = activeTool(node.id) ?? 'Working'
        emit(now, node.id, 'status changed', agent.status)
      }
      node.status = agent.status
      if (terminal.has(agent.status) || agent.status === 'idle') {
        node.endedAt ??= now
        life.endedAt = Math.max(life.endedAt ?? -Infinity, now)
        node.activity = agent.status === 'completed' ? 'Completed' : agent.status === 'failed' ? 'Failed' :
          agent.status === 'killed' ? 'Stopped' : 'Idle'
      } else if (agent.status === 'running') {
        if (node.endedAt !== undefined) { node.startedAt = now; node.endedAt = undefined }
        node.startedAt ??= now
        life.activeAt = Math.max(life.activeAt ?? -Infinity, now)
      } else if (agent.status === 'waiting') node.activity = 'Waiting'
    }
  },

  reset(): void {
    nodes.clear()
    events.length = 0
    routes.clear()
    pendingSpawns.clear()
    lifecycle.clear()
    startedSteps.clear()
    turnUsage.clear()
    failedTurns.clear()
    tools.clear()
    assistantBuffers.clear()
    activeTurns.clear()
    main = makeMain()
  },
}
