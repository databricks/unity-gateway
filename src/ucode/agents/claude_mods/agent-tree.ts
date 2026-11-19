import type { EngineInterface, On } from 'claude-code'
import { NATIVE_VIEW, pressSwitchedView, registerAgentView } from './agent-view.ts'
import { agentTree, activityMark, activityText, duration, safePreviewText, safeText, type TreeNode } from './agent-tree-state.ts'

const PANE_ID = 'agent-tree'
const PANE_TITLE = 'Subagents'
const OPEN_PANE = { id: PANE_ID, title: PANE_TITLE, closeOnEscape: true, rows: 46, columns: 104 } as const
export const AGENT_TREE_PANE = { ...OPEN_PANE, focus: true } as const
const OPEN_MANUALLY = AGENT_TREE_PANE
const ACTIVE = new Set(['pending', 'running', 'waiting'])
const FINISHED = new Set(['completed', 'idle', 'killed'])

let selected: string | undefined
let pinned: string | undefined
let page = 0
let showFinished = false
let displayOrder: string[] = []
let clock = 0
let refresh: { cancel: () => void } | undefined
let autoOpen = true
let autoOpenedBatch = false
let failedOnly = false
// The child whose tile holds the pane's focus ring, if any: the yellow
// selection follows the ring, so keyboard and mouse agree on one selection.
let focused: string | undefined

export function resetAgentTreeView(): void {
  refresh?.cancel()
  refresh = undefined
  selected = undefined
  pinned = undefined
  page = 0
  showFinished = false
  failedOnly = false
  displayOrder = []
  focused = undefined
}

export function clearAgentTree(): void {
  resetAgentTreeView()
  autoOpen = true
  autoOpenedBatch = false
  agentTree.reset()
}

export function resetAutoOpenBatchIfIdle(): void {
  if (![...agentTree.nodes.values()].some(node => ACTIVE.has(node.status))) autoOpenedBatch = false
}

export async function autoOpenTreeForNewBatch($: EngineInterface): Promise<void> {
  if (!autoOpen || autoOpenedBatch || ![...agentTree.nodes.values()].some(node => ACTIVE.has(node.status))) return
  autoOpenedBatch = true
  const existing = (await $.ui.panes()).find(pane => pane.id === PANE_ID)
  if (existing?.isShown) return
  if (existing) await $.ui.close({ id: PANE_ID })
  const opened = await $.ui.open(OPEN_PANE)
  if (opened.isPlaced) $.ui.invalidate('ui.render')
}

async function openTreeManually($: EngineInterface): Promise<boolean> {
  const existing = (await $.ui.panes()).find(pane => pane.id === PANE_ID)
  if (existing && !existing.isShown) await $.ui.close({ id: PANE_ID })
  return (await $.ui.open(OPEN_MANUALLY)).isPlaced
}

function modelLabel(model?: string): string {
  if (!model) return 'model unknown'
  return safeText(model.replace(/^system\.ai\./, '').replace(/\[1m\]$/, ''), 36)
}

function firstSentence(value?: string): string {
  if (!value) return ''
  const clean = safePreviewText(value, 32768)
  const match = clean.match(/^[\s\S]*?[.!?](?=\s|$)/)
  return match?.[0] ?? clean
}

function logTag(action: string): { text: string; state: 'running' | 'failed' | 'ok' | 'neutral' } {
  if (action === 'tool started') return { text: '[running]', state: 'running' }
  if (action === 'tool failed' || action === 'turn failed' || action === 'step failed' || action === 'spawn denied') {
    return { text: '[failed]', state: 'failed' }
  }
  if (action === 'tool finished') return { text: '[ok returned]', state: 'ok' }
  if (action === 'route selecting') return { text: '[selecting]', state: 'running' }
  if (action === 'route selected') return { text: '[selected]', state: 'ok' }
  if (action === 'final answer' || action === 'turn completed') return { text: '[done]', state: 'ok' }
  if (action === 'turn stopped') return { text: '[stopped]', state: 'neutral' }
  return { text: '[message]', state: 'neutral' }
}

function logText(event: { action: string; detail?: string }): string {
  if (event.action === 'route selected') return `Smart Router → ${modelLabel(event.detail)}`
  if (event.action === 'route selecting') return 'Smart Router selecting a model'
  if (event.action === 'agent started') return 'Subagent started'
  if (event.action === 'spawn requested') return 'Subagent requested'
  if (event.action === 'spawn denied') return 'Launch denied'
  if (event.action === 'tool started') return event.detail ?? 'Tool started'
  if (event.action === 'tool finished') return event.detail ? `${event.detail} returned` : 'Tool returned'
  if (event.action === 'tool failed') return event.detail ? `${event.detail} failed` : 'Tool failed'
  if (event.action === 'assistant message' || event.action === 'final answer') return event.detail ?? ''
  if (event.action === 'turn completed') return 'Finished task'
  if (event.action === 'turn failed') return 'Task failed'
  if (event.action === 'turn stopped') return 'Task stopped'
  if (event.action === 'step failed') return 'Model step failed'
  return ''
}

export function registerAgentTree(on: On): void {
  clearAgentTree()
  registerAgentView(on)
  let roster = ''

  on('command.run', { command: 'smart-router' }, async ($, e, next) => {
    const args = e.args.trim()
    if (args === 'tree off') {
      autoOpen = false
      return { text: 'Subagent auto-open is off for this session.' }
    }
    if (args === 'tree auto') {
      autoOpen = true
      autoOpenedBatch = [...agentTree.nodes.values()].some(node => ACTIVE.has(node.status))
      return { text: 'Subagent auto-open is on for this session.' }
    }
    if (args !== 'tree') return next(e)
    resetAgentTreeView()
    return await openTreeManually($)
      ? {}
      : { text: 'The subagent view is not visible on this surface.' }
  })

  on('turn.start', async ($, e, next) => {
    clock = await $.clock.now().catch(() => clock)
    agentTree.turnStarted(e, clock)
    if (e.agentId) await autoOpenTreeForNewBatch($).catch(() => undefined)
    $.ui.invalidate('ui.render')
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    clock = await $.clock.now().catch(() => clock)
    agentTree.toolStarted(e, clock)
    $.ui.invalidate('ui.render')
    try {
      const result = await next(e)
      clock = await $.clock.now().catch(() => clock)
      agentTree.toolFinished(e, result, clock)
      $.ui.invalidate('ui.render')
      return result
    } catch (error) {
      agentTree.toolFinished(e, { isError: true }, clock)
      $.ui.invalidate('ui.render')
      throw error
    }
  })

  on('ui.close', { id: PANE_ID }, ($, e, next) => {
    resetAgentTreeView()
    return next(e)
  })

  // Every ring move (the person's Tab, arrows or click, autoFocus, $.ui.focus)
  // passes here first; the yellow tile selection tracks the ring's element. The
  // engine redraws the site once the move lands, which re-runs the render above.
  on('ui.focus', { requestId: PANE_ID }, ($, e, next) => {
    // The engine names the element taking the ring `element`; the test kit
    // raises the move as the focus call's own args, where the field is `key`.
    const landed = e.element ?? (e as { key?: string }).key
    focused = landed?.startsWith('agent:') ? landed.slice('agent:'.length) : undefined
    return next(e)
  })

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE_ID || e.surface === 'mobile') return next(e)
    clock = await $.clock.now().catch(() => clock)
    const agents = await $.agent.list().catch(() => [])
    roster = JSON.stringify(agents)
    agentTree.syncAgents(agents, clock)
    resetAutoOpenBatchIfIdle()
    if (!refresh) refresh = $.clock.every(500, async () => {
      clock = await $.clock.now().catch(() => clock)
      const current = await $.agent.list().catch(() => [])
      const nextRoster = JSON.stringify(current)
      const changed = nextRoster !== roster
      roster = nextRoster
      agentTree.syncAgents(current, clock)
      resetAutoOpenBatchIfIdle()
      if (changed || [...agentTree.nodes.values()].some(node => ACTIVE.has(node.status)) ||
          [...agentTree.routes.values()].some(route => route.status === 'selecting')) $.ui.invalidate('ui.render')
    })

    const motionSetting = await $.settings.read({ key: 'prefersReducedMotion' })
    const preference = typeof motionSetting === 'boolean' ? motionSetting : motionSetting?.prefersReducedMotion
    const reducedMotion = preference === true
    const theme = (await $.config.list()).find(row => row.key === 'theme')?.value
    const light = typeof theme === 'string' && theme.startsWith('light')
    const palette = light ? {
      background: '#f4f5f7', surface: '#ffffff', foreground: '#20242b', muted: '#626a76',
      line: '#bcc2cc', main: '#8a4b00', router: '#006b5c', running: '#006b5c', finished: '#276b3a', error: '#af2548',
      select: '#eab308', selectText: '#854d0e', selectTint: '#fefce8',
    } : {
      background: '#111318', surface: '#1d2229', foreground: '#f2f3f5', muted: '#a0a6b2',
      line: '#515966', main: '#f4b56a', router: '#54d6bd', running: '#54d6bd', finished: '#8fd3a8', error: '#ff7285',
      select: '#ffd60a', selectText: '#ffd60a', selectTint: '#2a2513',
    }
    const colorFor = (status: string) => status === 'failed' ? palette.error : status === 'running' ? palette.running
      : status === 'completed' ? palette.finished : palette.muted
    const { Box, Text, Button } = $.ui.resolve(e)
    const width = Math.max(20, (e.props.bodyColumns ?? e.viewport?.columns ?? 60) - 4)
    const bodyRows = Math.max(1, e.props.scroll?.bodyRows ?? ((e.viewport?.rows ?? 40) - 4))
    const nodes = [...agentTree.nodes.values()]
    const nodeById = new Map(nodes.map(node => [node.id, node]))
    const active = nodes.filter(node => ACTIVE.has(node.status))
    const failed = nodes.filter(node => node.status === 'failed' || node.errorCount > 0)
    const done = nodes.filter(node => node.status === 'completed').length
    const nowMark = (node: TreeNode) => node.status === 'running' && !reducedMotion
      ? activityMark(node, clock) : node.status === 'running' ? '●' : activityMark(node, clock)
    const text = (value: string, color = palette.foreground, bold = false, wrap: 'wrap' | 'truncate-end' = 'truncate-end') =>
      Text({ color, bold, wrap, children: [value] })
    const compact = width < 40 || bodyRows < 18
    const tiny = bodyRows < 8
    const attention = (node: TreeNode) => node.status === 'failed' || node.errorCount > 0
    const viewing = (e.props as { view?: { agentId?: string } }).view?.agentId
    const viewName = viewing ? nodeById.get(viewing)?.name : undefined
    // A tile press reaches the host's view switch through registerAgentView's
    // press hook; a denied switch (or a host without the switch) lands here in
    // the read-only in-pane detail instead.
    const openDetail = (id: string) => { selected = id; $.ui.invalidate('ui.render') }
    const focusRingOn = (items: VisibleNode[], index: number) => {
      const target = items[Math.max(0, Math.min(index, items.length - 1))]
      if (target) $.ui.focus({ requestId: PANE_ID, key: 'agent:' + target.node.id }).catch(() => undefined)
    }

    if (!displayOrder.length) displayOrder = nodes.map(node => node.id)
    for (const node of nodes) if (!displayOrder.includes(node.id)) displayOrder.push(node.id)
    displayOrder = displayOrder.filter(id => nodeById.has(id))
    const order = new Map(displayOrder.map((id, index) => [id, index]))
    const children = new Map<string, TreeNode[]>()
    const roots: TreeNode[] = []
    const unknown: TreeNode[] = []
    for (const node of nodes) {
      if (node.parentId === 'main') roots.push(node)
      else if (node.parentId && nodeById.has(node.parentId)) {
        const list = children.get(node.parentId) ?? []
        list.push(node)
        children.set(node.parentId, list)
      } else unknown.push(node)
    }
    const sortSiblings = (list: TreeNode[]) => list.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))
    sortSiblings(roots)
    sortSiblings(unknown)
    for (const list of children.values()) sortSiblings(list)
    type VisibleNode = { node: TreeNode; depth: number; parent?: TreeNode; connected: boolean }
    const all: VisibleNode[] = []
    const visit = (node: TreeNode, depth: number, parent?: TreeNode, seen = new Set<string>()) => {
      if (seen.has(node.id)) return
      seen.add(node.id)
      all.push({ node, depth, parent, connected: true })
      for (const child of children.get(node.id) ?? []) visit(child, depth + 1, node, seen)
    }
    for (const root of roots) visit(root, 0)
    for (const node of unknown) if (!all.some(row => row.node.id === node.id)) {
      all.push({ node, depth: 0, connected: false })
      for (const child of children.get(node.id) ?? []) visit(child, 1, node)
    }
    for (const node of nodes) if (!all.some(row => row.node.id === node.id)) all.push({ node, depth: 0, connected: false })
    const hiddenFinished = nodes.length >= 12 && !showFinished
      ? all.filter(row => FINISHED.has(row.node.status) && !attention(row.node) && !(children.get(row.node.id)?.length)).length : 0
    const visible = all.filter(row => (!failedOnly || attention(row.node)) &&
      (!hiddenFinished || !FINISHED.has(row.node.status) || attention(row.node) || children.get(row.node.id)?.length))
    const pageSize = 12
    const pageCount = Math.max(1, Math.ceil(visible.length / pageSize))
    page = Math.min(page, pageCount - 1)
    const pageItems = visible.slice(page * pageSize, (page + 1) * pageSize)
    const pageIds = new Set(pageItems.map(item => item.node.id))
    const routeEvents = [...agentTree.routes.values()].filter(route => !route.agentId && route.status !== 'selected').slice(-3)
    const history = agentTree.events.filter(event => event.agentId !== 'main' && logText(event))
    const recent = history.filter(event => !pinned || event.agentId === pinned).slice(-6)
    const latestMessages = nodes.flatMap(node => {
      if (pinned ? node.id !== pinned : pageIds.size > 0 && !pageIds.has(node.id)) return []
      const message = [...history].reverse().find(event => event.agentId === node.id &&
        (event.action === 'assistant message' || event.action === 'final answer'))
      return message ? [message] : node.assistantText ? [{
        at: node.activityAt ?? node.startedAt ?? 0, agentId: node.id,
        action: node.status === 'completed' ? 'final answer' : 'assistant message', detail: node.assistantText,
      }] : []
    }).sort((a, b) => (order.get(a.agentId) ?? 0) - (order.get(b.agentId) ?? 0))
    const followedName = pinned ? nodeById.get(pinned)?.name : undefined
    const pinButton = pinned
      ? Button({ key: 'follow-latest', label: 'All updates', plain: true, dimColor: true, onPress: () => { pinned = undefined; $.ui.invalidate('ui.render') } })
      : undefined
    const completionStatus = failed.length
      ? `${done} done · ${failed.length} ${failed.length === 1 ? 'needs' : 'need'} attention`
      : nodes.length ? `${done} done` : 'Waiting'
    const headerStatus = active.length
      ? `${active.length} active${failed.length ? ` · ${failed.length} flagged` : ''}`
      : completionStatus
    const header = Box({ flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'space-between', marginBottom: compact ? 0 : 1, children: [
      Box({ flexDirection: 'row', flexWrap: 'wrap', columnGap: 2, children: [text('SUBAGENTS', palette.foreground, true),
        text(headerStatus, active.length ? palette.running : palette.muted),
        ...(pinButton ? [pinButton] : []),
      ]}),
      ...(!tiny ? [Button({ key: 'auto-open', label: `Auto ${autoOpen ? 'on' : 'off'}`, plain: true, dimColor: true,
        onPress: () => {
          autoOpen = !autoOpen
          if (autoOpen) autoOpenedBatch = active.length > 0
          $.ui.invalidate('ui.render')
        } })] : []),
      ...(viewing ? [Button({ key: 'back-to-main', label: 'Back to main', plain: true,
        onPress: () => { $.ui.invalidate('ui.render') } })] : []),
      Button({ key: 'close-tree', label: 'Close', plain: true, ...(e.props.isFocused ? { hotkey: 'q' } : {}),
        onPress: () => { resetAgentTreeView(); return $.ui.close({ id: PANE_ID }) } }),
    ] })

    const roomy = nodes.length <= 3 && !compact
    const directCards = !pageItems.some(item => item.depth > 0 || !item.connected)
    const columns = !roomy && !compact && directCards && width >= 68 ? 2 : 1
    const cardWidth = Math.floor((width - (columns - 1) * 2) / columns)
    const toolSteps = (id: string) => history.filter(event => event.agentId === id &&
      (event.action === 'tool finished' || event.action === 'tool failed')).slice(-2)
    const stateLabel = (node: TreeNode) => ({
      running: 'Running', pending: 'Starting', waiting: 'Waiting', completed: 'Finished',
      failed: 'Failed', killed: 'Stopped', idle: 'Idle',
    })[node.status]
    // True while the ring sits on a tile that is drawn: then the person's
    // position wins. Otherwise the first tile offers autoFocus again, so a
    // keyboard take always has somewhere to start.
    const ringPresent = focused !== undefined && pageItems.some(item => item.node.id === focused)
    const tile = ({ node, depth, parent, connected }: VisibleNode, tileIndex: number) => {
      const stateColor = colorFor(node.status)
      const ringed = e.props.isFocused === true && focused === node.id
      const inView = viewing === node.id
      const mark = nowMark(node)
      const elapsed = node.startedAt !== undefined ? duration((node.endedAt ?? clock) - node.startedAt) : ''
      const name = safeText(node.name || 'Unnamed', 32)
      const task = safePreviewText(node.task, 2048) || 'Task not reported'
      const indent = Math.min(depth * 2, Math.floor(width / 4))
      const title = Box({ flexDirection: compact && !tiny ? 'column' : 'row', justifyContent: 'space-between', children: [
        Button({ key: 'agent:' + node.id, label: name, plain: true, hover: { bold: true },
          ...(e.props.isFocused && pageItems.length <= 9
            ? { hotkey: String(tileIndex + 1) } : {}),
          ...(tileIndex === 0 && !ringPresent ? { autoFocus: true } : {}),
          onPress: () => { if (!pressSwitchedView(node.id)) openDetail(node.id) } }),
        Box({ flexDirection: 'row', columnGap: 1, children: [
          Button({ key: 'details:' + node.id, label: 'details', plain: true, dimColor: true,
            onPress: () => { selected = node.id; $.ui.invalidate('ui.render') } }),
          text(`${mark} ${stateLabel(node)}${elapsed ? ' · ' + elapsed : ''}`, stateColor),
          ...(inView ? [text('· in view', palette.selectText, true)] : []),
        ] }),
      ] })
      const steps = toolSteps(node.id)
      const current = ACTIVE.has(node.status) ? `Now · ${activityText(node)}` :
        node.status === 'failed' ? 'Task failed' : node.status === 'killed' ? 'Task stopped' : ''
      const statement = firstSentence(node.assistantText)
      const card = Box({ key: node.id, width: Math.max(16, cardWidth - indent), flexGrow: 1,
        flexDirection: 'column', paddingX: compact ? 0 : 1,
        backgroundColor: ringed ? palette.selectTint : palette.surface,
        ...(!tiny ? { borderStyle: 'round', borderColor: ringed ? palette.select :
          node.status === 'running' || node.status === 'failed' ? stateColor : palette.line } : {}),
        hover: { borderColor: ringed ? palette.select : palette.foreground }, children: [
          title,
          text(task, palette.foreground, true, 'wrap'),
          ...(parent ? [text(`↳ spawned by ${safeText(parent.name || parent.role, 32)}`, palette.muted, false, 'wrap')] :
            !connected ? [text('NOT CONNECTED · parent unknown', palette.muted, false, 'wrap')] : []),
          ...(current ? [text(current, stateColor, false, 'wrap')] : []),
          ...((tiny ? [] : roomy ? steps : steps.slice(-1)).map(step =>
            text(`${step.action === 'tool failed' ? '×' : '✓'} ${step.detail} · ${step.action === 'tool failed' ? 'failed' : 'done'}`,
              step.action === 'tool failed' ? palette.error : palette.muted, false, 'wrap'))),
          ...(roomy && statement ? [text(statement, palette.foreground, false, 'wrap')] : []),
        ],
      })
      return Box({ key: 'tile:' + node.id, flexDirection: 'column', marginLeft: indent, children: [card] })
    }
    // Siblings share a labelled group; no connector ever runs from one tile into another.
    const tileGrid = Box({ flexDirection: columns > 1 ? 'row' : 'column',
      ...(columns > 1 ? { flexWrap: 'wrap' as const, columnGap: 2 } : {}), rowGap: 1,
      children: pageItems.map((item, index) => tile(item, index)) })

    const logRow = (event: typeof agentTree.events[number]) => {
      const node = nodeById.get(event.agentId)
      const name = safeText(node?.name || 'Subagent', 32)
      const tag = logTag(event.action)
      const message = event.action === 'assistant message' || event.action === 'final answer'
      const detail = logText(event)
      return Box({ key: event.streamKey || `${event.agentId}:${event.action}:${event.at}`,
        flexDirection: 'column', marginTop: 1, children: [
          Box({ flexDirection: 'row', columnGap: 1, children: [
            text(name, palette.foreground, true),
            text(event.action === 'final answer' ? '· result' : message ? '· says' : tag.text,
              tag.state === 'failed' ? palette.error : palette.muted),
          ] }),
          text(detail, palette.foreground, false, 'wrap'),
        ] })
    }
    const logRows = (pinned
      ? [...latestMessages, ...recent.filter(event => !latestMessages.includes(event))].sort((a, b) => a.at - b.at)
      : latestMessages.length ? latestMessages.slice(-12) : recent).map(event => logRow(event))
    const routeRows = routeEvents.filter(route => !route.supersededBy).map(route => Box({ key: 'route:' + route.toolId, flexDirection: 'column', marginTop: 1, children: [
      text(safeText(route.name, 24), palette.router, true),
      text(safePreviewText(route.task, 2048) || 'Task not reported', palette.foreground, false, 'wrap'),
      text(route.status === 'denied' ? '[launch denied]' : route.status === 'selecting' ? '[selecting]' : '[not launched]',
        route.status === 'denied' ? palette.error : palette.muted),
    ] }))

    const pager = pageCount > 1 ? Box({ flexDirection: 'row', columnGap: 1, children: [
      ...(page > 0 ? [Button({ key: 'previous', label: '‹', plain: true, ...(e.props.isFocused ? { hotkey: 'p' } : {}),
        onPress: () => {
          const preceding = visible.slice((page - 1) * pageSize, page * pageSize)
          page--
          // The selection follows the page turn at once; the ring request then
          // points the engine's keyboard focus at the same tile.
          focused = preceding[0]?.node.id
          $.ui.invalidate('ui.render')
          focusRingOn(preceding, 0)
        } })] : []),
      text(`${page + 1}/${pageCount}`, palette.muted),
      ...(page + 1 < pageCount ? [Button({ key: 'next', label: '›', plain: true, ...(e.props.isFocused ? { hotkey: 'n' } : {}),
        onPress: () => {
          const following = visible.slice((page + 1) * pageSize, (page + 2) * pageSize)
          page++
          focused = following[0]?.node.id
          $.ui.invalidate('ui.render')
          focusRingOn(following, 0)
        } })] : []),
    ] }) : undefined
    const summary = `subagents ${done}/${nodes.length} done · ${failed.length} ${failed.length === 1 ? 'needs' : 'need'} attention`
    const routed = agentTree.routes.size > 0
    const routerLabel = routed ? 'Smart Router' : 'Subagent delegation'
    const treeTitle = Box({ flexDirection: 'row', flexWrap: 'wrap', columnGap: 1, marginBottom: 1, children: [
      text('Main session', palette.main, true), text('→', palette.line),
      text(routerLabel, palette.router, true),
      text(directCards ? '→ Parallel subagents' : '→ Agent tree', palette.muted),
    ] })
    const logTitle = Box({ flexDirection: compact ? 'column' : 'row', justifyContent: 'space-between', marginTop: compact ? 0 : 1,
      children: [text('SESSION LOG', palette.foreground, true),
        text(followedName ? `Pinned · ${safeText(followedName, 28)}` : 'Latest from each agent', palette.muted)] })
    const returnRow = text('↩ Results return to Main', palette.muted)
    const hint = tiny ? undefined : text(viewing
      ? `Viewing ${safeText(viewName || 'subagent', 28)} · Esc returns to Main`
      : e.props.isFocused
        ? NATIVE_VIEW ? 'Tab picks a tile · Enter steps into it · Esc closes'
          : 'Tab picks a tile · Enter opens its details · Esc closes'
        : NATIVE_VIEW ? 'ctrl+x tab focuses tiles · or click a tile to step in'
          : 'ctrl+x tab focuses tiles · or click a tile for details', palette.muted)
    const noActivity = nodes.length === 0 && routeRows.length === 0
      ? [text('Subagent activity will appear here.', palette.muted)] : []
    const logContent = logRows.length ? logRows : [text('Waiting for the first subagent event.', palette.muted)]

    if (selected) {
      const detail = nodeById.get(selected)
      if (detail) {
        const detailText = safePreviewText(detail.assistantText, 32768)
        return Box({ flexDirection: 'column', backgroundColor: palette.background, paddingX: 1, children: [
          Box({ flexDirection: 'row', justifyContent: 'space-between', children: [
            Button({ key: 'all-events', label: 'Back to all subagents', plain: true, onPress: () => { selected = undefined; $.ui.invalidate('ui.render') } }),
            Button({ key: 'agent:' + detail.id, label: 'Open transcript', plain: true,
              onPress: () => { if (!pressSwitchedView(detail.id)) $.ui.invalidate('ui.render') } }),
            Button({ key: 'close-tree', label: 'Close', plain: true, ...(e.props.isFocused ? { hotkey: 'q' } : {}),
              onPress: () => { resetAgentTreeView(); return $.ui.close({ id: PANE_ID }) } }),
          ] }),
          text(`${detail.name || 'Subagent'} · ${detail.role || 'role unknown'} · ${modelLabel(detail.model)} · effort ${detail.effort || 'unknown'}`, palette.foreground, true),
          text(safePreviewText(detail.task, 32768), palette.muted, false, 'wrap'),
          text(`Now · ${activityText(detail)} · ${detail.toolCount} tools${detail.errorCount ? ` · ${detail.errorCount} errors` : ''}`, colorFor(detail.status), false, 'wrap'),
          text(detail.status === 'completed' ? 'FINAL ANSWER' : 'SAYING NOW', palette.muted, true),
          text(detailText || 'No assistant message captured yet.', palette.foreground, false, 'wrap'),
          Button({ key: 'pin-current', label: 'Pin this agent in the live log', plain: true,
            onPress: () => { pinned = detail.id; selected = undefined; $.ui.invalidate('ui.render') } }),
          text('RECENT ACTIVITY', palette.muted, true),
          ...history.filter(event => event.agentId === detail.id &&
            (!['assistant message', 'final answer'].includes(event.action) || event.detail !== detail.assistantText)).slice(-12).map(event => logRow(event)),
          ...([...agentTree.routes.values()].some(route => route.supersededBy === detail.id)
            ? [text('Relaunched after a denied routing attempt', palette.muted, false, 'wrap')] : []),
          text(`Launch model · ${detail.model || 'not reported'}`, palette.muted, false, 'wrap'),
          text(`Parent · ${detail.parentId === 'main' ? 'Main session' : detail.parentId ? nodeById.get(detail.parentId)?.name || detail.parentId : 'not reported'}`, palette.muted, false, 'wrap'),
          ...([...agentTree.routes.values()].filter(route => route.agentId === detail.id).flatMap(route => [
            text(`Router selection · ${route.model || 'not reported'}`, palette.muted, false, 'wrap'),
            ...(route.rationale ? [text(safePreviewText(route.rationale, 32768), palette.muted, false, 'wrap')] : []),
          ])),
          text(`Usage · ${detail.inputTokens ?? 'unknown'} input · ${detail.outputTokens ?? 'unknown'} output tokens`, palette.muted, false, 'wrap'),
          text(`ID · ${detail.id}`, palette.muted, false, 'wrap'),
        ] })
      }
      selected = undefined
    }

    return Box({ flexDirection: 'column', backgroundColor: palette.background,
      ...(e.props.placement === 'dock' ? { minHeight: bodyRows } : {}), paddingX: compact ? 1 : 2,
      children: [header, ...(hint ? [hint] : []), ...(!tiny ? [treeTitle] : []),
        ...(tiny ? [] : failed.slice(0, 3)).map(node => Button({ key: 'attention:' + node.id, plain: true,
          label: `! ${node.name || 'Subagent'} · needs attention`, onPress: () => { selected = node.id; $.ui.invalidate('ui.render') } })),
        tileGrid, ...routeRows, ...noActivity,
        ...(pager ? [pager] : []),
        ...(hiddenFinished ? [Button({ key: 'show-finished', label: `${hiddenFinished} finished · show`, plain: true,
          onPress: () => { showFinished = true; $.ui.invalidate('ui.render') } })] : []),
        logTitle, ...logContent, returnRow,
        Box({ flexDirection: 'row', flexWrap: 'wrap', columnGap: 2, marginTop: 1, children: [
          Button({ key: 'show-active', label: 'Active first', plain: true, dimColor: true,
            onPress: () => { displayOrder.sort((a, b) => Number(ACTIVE.has(nodeById.get(b)!.status)) - Number(ACTIVE.has(nodeById.get(a)!.status))); page = 0; $.ui.invalidate('ui.render') } }),
          Button({ key: 'show-failed', label: failedOnly ? 'All subagents' : 'Failed subagents', plain: true, dimColor: true,
            onPress: () => { failedOnly = !failedOnly; page = 0; $.ui.invalidate('ui.render') } }),
        ] }),
        Box({ flexDirection: 'row', marginTop: compact ? 0 : 1, children: [text(summary, palette.muted, false, 'wrap')] }),
      ] })
  })
}
