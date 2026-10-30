import { atom, read, update } from 'claude-code'
import type { EngineInterface, On, PluginState } from 'claude-code'

type Subagent = PluginState['ug-smart-router']['paneAgents'][number]

const PANE = 'subagents'
const REFRESH_INTERVAL_MS = 1000
const CLOSE_DELAY_MS = 5000
// shortcut: track observed spawns only; hydrate from agent.list when resume/reload recovery is needed.
const agents = atom({ plugin: 'ug-smart-router', key: 'paneAgents' } as const, [] as Subagent[])
const isCollapsed = atom({ plugin: 'ug-smart-router', key: 'paneCollapsed' } as const, false)
const now = atom({ plugin: 'ug-smart-router', key: 'paneNow' } as const, 0)
// Waiting agents can still have background work after their turn ends.
const FINISHED = new Set(['completed', 'failed', 'killed', 'idle'])
let closeAt: number | undefined

const elapsed = (agent: Subagent, at: number) => {
  const seconds = Math.round((agent.isDone ? agent.durationMs ?? 0 : Math.max(0, at - agent.startedAt)) / 1000)
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`
}

async function openPane($: EngineInterface, collapsed = false) {
  // Claude's minimum dock width is 24 columns including its one-column divider.
  return $.ui.open({ id: PANE, title: 'Subagents', columns: collapsed ? 23 : 34 })
}

async function setCollapsed($: EngineInterface, collapsed: boolean) {
  await update($, isCollapsed, () => collapsed)
  // Releasing focus lets Ctrl+X, Tab enter the compact pane again to expand it.
  if (collapsed) await $.ui.close({ id: PANE })
  await openPane($, collapsed)
}

async function reconcile($: EngineInterface) {
  if (await read($, isCollapsed) && (await $.ui.panes()).some(pane => pane.id === PANE && pane.isFocused)) {
    await setCollapsed($, false)
  }
  const before = await read($, agents)
  if (!before.some(agent => !agent.isDone)) {
    if (closeAt !== undefined && await $.clock.now() >= closeAt) {
      closeAt = undefined
      if ((await read($, agents)).every(agent => agent.isDone)) {
        await $.ui.close({ id: PANE })
      }
    }
    return
  }
  closeAt = undefined
  const status = new Map((await $.agent.list()).map(info => [info.id, info.status as string]))
  const at = await $.clock.now()
  await update($, agents, all => all.map(agent => {
    const word = status.get(agent.id)
    if (word === undefined) return agent
    const isDone = FINISHED.has(word)
    const isError = word === 'failed' || word === 'killed'
    if (isDone === agent.isDone && isError === agent.isError) return agent
    return isDone
      ? { ...agent, isDone, isError, tool: undefined, durationMs: Math.max(0, at - agent.startedAt) }
      : { ...agent, isDone, isError, durationMs: undefined }
  }))
  await update($, now, () => at)
  if ((await read($, agents)).every(agent => agent.isDone)) {
    closeAt = at + CLOSE_DELAY_MS
  }
}

export const registerSubagentPane = (on: On): void => {
  on('session.start', { surface: 'terminal' }, async ($, e, next) => {
    closeAt = undefined
    await update($, agents, () => [])
    await update($, now, () => 0)
    await update($, isCollapsed, () => false)
    await $.command.register({
      name: 'subagents',
      description: 'Open the subagent side pane; run again to collapse or expand it',
    })
    $.clock.every(REFRESH_INTERVAL_MS, () => reconcile($))
    return next(e)
  })

  on('command.run', { command: 'subagents' }, async $ => {
    const isUp = (await $.ui.panes()).some(pane => pane.id === PANE && pane.isPlaced)
    const wasCollapsed = await read($, isCollapsed)
    const collapsed = isUp && !wasCollapsed
    await setCollapsed($, collapsed)
    const state = collapsed ? 'collapsed' : isUp || wasCollapsed ? 'expanded' : 'opened'
    return { text: `Subagent pane ${state}.` }
  })

  on('agent.spawn', async ($, e, next) => {
    const started = await next(e)
    if (started.deny !== undefined || !started.agentId) return started
    const at = await $.clock.now()
    const agent: Subagent = {
      id: started.agentId,
      type: e.subagentType,
      description: e.description,
      model: /haiku|sonnet|opus|fable|mythos/.exec(started.model)?.[0] ?? started.model,
      startedAt: at,
      isDone: false,
      isError: false,
    }
    await update($, now, () => at)
    const newBatch = !(await read($, agents)).some(one => !one.isDone)
    const expand = newBatch && await read($, isCollapsed)
    if (newBatch) {
      await update($, isCollapsed, () => false)
    }
    await update($, agents, list => {
      const kept = list.some(one => !one.isDone) ? list : []
      return [...kept.filter(one => one.id !== agent.id), agent]
    })
    if (expand || !await read($, isCollapsed) && !(await $.ui.panes()).some(pane => pane.id === PANE && pane.isPlaced)) {
      await openPane($)
    }
    return started
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId) {
      await update($, agents, all => all.map(agent => agent.id === e.agentId
        ? { ...agent, isDone: false, isError: false, durationMs: undefined, tool: e.tool }
        : agent))
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const list = await read($, agents)
    await read($, now)
    const at = await $.clock.now()
    const running = list.filter(agent => !agent.isDone)
    const finished = list.filter(agent => agent.isDone)
    const errors = finished.filter(agent => agent.isError).length
    const done = finished.length - errors
    if (await read($, isCollapsed)) {
      return Box({ flexDirection: 'column', children: [
        Text({ children: ['Focus to expand'] }),
        Text({ dimColor: true, children: ['Ctrl+X, then Tab'] }),
        Text({ color: 'yellow', children: [`● ${running.length} running`] }),
        Text({ color: 'green', children: [`✓ ${done} done`] }),
        ...(errors ? [Text({ color: 'red', children: [`● ${errors} failed`] })] : []),
      ] })
    }

    const ordered = [...running.reverse(), ...finished.reverse()]
    const room = Math.max(1, Math.floor(((e.viewport?.rows ?? 24) - 7) / 4))
    const viewing = e.props.view?.agentId
    const show = async (agentId?: string) => {
      const moved = await $.ui.view(agentId ? { agentId } : {})
      if (moved.deny) $.ui.toast(`Can't open that transcript: ${moved.deny}`)
    }
    const visible = ordered.slice(0, room)
    return Box({ flexDirection: 'column', children: [
      Text({ children: ['Focus: Ctrl+X, then Tab'] }),
      Text({ dimColor: true, children: ['↑/↓ move · Enter or 1–9 open'] }),
      Text({ dimColor: true, children: ['0 main · Esc prompt · s collapse'] }),
      Box({ justifyContent: 'space-between', children: [
        Text({ dimColor: true, children: [
          `${running.length} running · ${done} done${errors ? ` · ${errors} failed` : ''}`,
        ] }),
        Button({ key: 'collapse', label: '>>', hotkey: 's', onPress: () => setCollapsed($, true) }),
      ] }),
      Button({ key: 'main', plain: true, hotkey: '0', autoFocus: !viewing || undefined,
        label: viewing ? '  main' : '▸ main', onPress: () => show() }),
      ...(list.length === 0 ? [Text({ dimColor: true, children: ['No subagents yet.'] })] : []),
      ...visible.map((agent, i) => Box({ key: agent.id, flexDirection: 'column', marginTop: 1, children: [
        Box({ flexDirection: 'row', children: [
          Text({ color: agent.isError ? 'red' : agent.isDone ? 'green' : 'yellow',
            children: [agent.isError ? '● error ' : agent.isDone ? '✓ ' : '● '] }),
          Button({ key: `view-${agent.id}`, plain: true, hotkey: i < 9 ? String(i + 1) : undefined,
            autoFocus: viewing === agent.id || undefined, onPress: () => show(agent.id),
            label: `${viewing === agent.id ? '▸' : ' '} ${agent.type}` }),
        ] }),
        Text({ dimColor: true, wrap: 'truncate-end', children: [`   ${agent.description}`] }),
        Text({ dimColor: true, wrap: 'truncate-end', children: [
          `   → ${agent.model} · ${elapsed(agent, at)}${agent.tool ? ` · ${agent.tool}` : ''}`,
        ] }),
      ] })),
      ...(ordered.length > room ? [Text({ dimColor: true, children: [`+${ordered.length - room} more`] })] : []),
    ] })
  })
}
