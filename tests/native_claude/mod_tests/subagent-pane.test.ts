import { expect, test } from 'claude-code/testing'

const pane = {
  plugin: 'ug-smart-router', component: 'Pane', surface: 'terminal',
  requestId: 'subagents', viewport: { columns: 34, rows: 28 }, props: {},
} as const

test('collapse keeps a compact side pane and focusing it expands it', async ($, on) => {
  on('clock.now', () => ({ value: 1000 }))
  const opened: any[] = []
  let placed = false
  let focused = false
  let fire: () => void = () => {}
  let scheduled: (() => void) | undefined
  const closed: any[] = []
  on('ui.panes', () => ({ value: placed ? [{ id: 'subagents', isPlaced: true, isFocused: focused }] : [] }))
  on('ui.open', ($, e) => { opened.push(e); placed = true; return { value: undefined } })
  on('ui.close', ($, e) => { closed.push(e); placed = false; return { value: undefined } })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['native prompt'] }))
  on('command.register', () => ({ value: undefined }))
  on('clock.every', () => new Promise(resolve => {
    fire = () => resolve({ value: undefined })
    scheduled?.()
  }))
  on('agent.list', () => ({ value: [{ id: 'child-1', status: 'waiting' }] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('agent.spawn', () => ({ agentId: 'child-1', model: 'sonnet' }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(opened).toEqual([])
  const empty = await $.ui.mount(pane)
  expect(await empty.find({ type: 'Text', text: 'Ctrl+X, then Tab to focus' })).toBeDefined()
  expect(await empty.find({ type: 'Text', text: '↑/↓ choose · Enter opens' })).toBeDefined()
  expect(await empty.find({ type: 'Text', text: 'No subagents yet.' })).toBeDefined()
  await empty.unmount()

  expect((await $.command.run({ command: 'subagents', args: '' })).text).toBe('Subagent pane opened.')
  expect(opened[0]).toMatchObject({ id: 'subagents', columns: 34 })
  await $.agent.spawn({ subagentType: 'Explore', description: 'Inspect', prompt: 'Inspect' })
  expect((await $.command.run({ command: 'subagents', args: '' })).text).toBe('Subagent pane collapsed.')
  expect(closed.map(event => event.id)).toEqual(['subagents'])
  expect(placed).toBe(true)
  expect(opened[1]).toMatchObject({ id: 'subagents', columns: 23 })
  const collapsed = await $.ui.mount({ ...pane, viewport: { columns: 23, rows: 28 } })
  expect(await collapsed.find({ type: 'Text', text: 'Focus to expand' })).toBeDefined()
  expect(await collapsed.find({ type: 'Text', text: 'Ctrl+X, then Tab' })).toBeDefined()
  expect(await collapsed.find({ type: 'Text', text: '● 1 running' })).toMatchObject({ props: { color: 'yellow' } })
  expect(await collapsed.find({ type: 'Text', text: '✓ 0 done' })).toMatchObject({ props: { color: 'green' } })
  expect(await collapsed.find({ type: 'Button' })).toBeUndefined()
  await collapsed.unmount()
  await $.agent.spawn({ subagentType: 'Explore', description: 'Inspect', prompt: 'Inspect' })
  expect(opened).toHaveLength(2)
  const tick = async () => {
    const nextTick = new Promise<void>(resolve => { scheduled = resolve })
    fire()
    await nextTick
    scheduled = undefined
  }
  await tick()
  expect(opened).toHaveLength(2)
  focused = true
  await tick()
  expect(opened[2]).toMatchObject({ id: 'subagents', columns: 34 })
  const expanded = await $.ui.mount(pane)
  expect(await expanded.find({ type: 'Text', text: 'Ctrl+X, then Tab to focus' })).toBeDefined()
  expect(await expanded.find({ type: 'Text', text: 'Focus to expand' })).toBeUndefined()
  await expanded.unmount()
})

test('pane tracks successful spawns, ignores denied spawns, and shows tools and elapsed time', async ($, on) => {
  let at = 1000
  const opened: any[] = []
  let placed = false
  on('ui.panes', () => ({ value: placed ? [{ id: 'subagents', isPlaced: true }] : [] }))
  on('ui.open', ($, e) => { opened.push(e); placed = true; return { value: undefined } })
  on('clock.now', () => ({ value: at }))
  on('agent.spawn', ($, e) => e.description === 'denied'
    ? { deny: 'blocked' }
    : { agentId: 'child-1', model: 'claude-sonnet-4-5-20250929' })
  on('tool.call', () => ({ result: 'ok' }))
  await $.agent.spawn({ subagentType: 'Explore', description: 'denied', prompt: 'denied' })
  expect(opened).toEqual([])
  const subagentType = 'ug-smart-router:ucode-route-claude-sonnet-5-5-1m-16041e2f'
  await $.agent.spawn({ subagentType, description: 'Inspect parser', prompt: 'Inspect parser' })
  expect(opened).toHaveLength(1)
  expect(opened[0]).toMatchObject({ id: 'subagents', columns: 34 })
  await $.agent.spawn({ subagentType, description: 'Inspect parser', prompt: 'Inspect parser' })
  expect(opened).toHaveLength(1)
  await $.tool.call({ tool: 'Read', agentId: 'child-1', file_path: '/work/parser.py' })
  at = 62000
  const ui = await $.ui.mount(pane)
  expect(await ui.find({ type: 'Text', text: '1 running' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '● ' })).toMatchObject({ props: { color: 'yellow' } })
  expect(JSON.stringify(await ui.drawn())).toContain('"label":"Inspect parser"')
  expect(JSON.stringify(await ui.drawn())).not.toContain('Back to main session')
  expect(JSON.stringify(await ui.drawn())).toContain('"label":"Collapse"')
  expect(JSON.stringify(await ui.drawn())).not.toContain(subagentType)
  expect(await ui.find({ type: 'Text', text: '   → sonnet · 1m01s · Read' })).toBeDefined()
  expect(JSON.stringify(await ui.drawn())).not.toContain('denied')
  await ui.unmount()
  const transcript = await $.ui.mount({ ...pane, props: { view: { agentId: 'child-1' } } })
  expect(JSON.stringify(await transcript.drawn())).toContain('"label":"Back to main session"')
  await transcript.unmount()
})

test('pane lingers five seconds after completion, cancels closing for new work, and reopens', async ($, on) => {
  let at = 1000
  let status = 'waiting'
  let peerStatus = 'waiting'
  let nextStatus = 'waiting'
  let placed = false
  const opened: any[] = []
  const closed: any[] = []
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['native prompt'] }))
  let fire: () => void = () => {}
  let scheduled: (() => void) | undefined
  on('ui.panes', () => ({ value: placed ? [{ id: 'subagents', isPlaced: true }] : [] }))
  on('ui.open', ($, e) => { opened.push(e); placed = true; return { value: undefined } })
  on('ui.close', ($, e) => { closed.push(e); placed = false; return { value: undefined } })
  on('clock.now', () => ({ value: at }))
  on('clock.every', () => new Promise(resolve => {
    fire = () => resolve({ value: undefined })
    scheduled?.()
  }))
  on('agent.list', () => ({ value: [
    { id: 'child-1', status }, { id: 'peer', status: peerStatus }, { id: 'child-2', status: nextStatus },
  ] }))
  on('agent.spawn', ($, e) => ({ agentId: e.description, model: 'custom-model' }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: '/work' }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const tick = async () => {
    const nextTick = new Promise<void>(resolve => { scheduled = resolve })
    fire()
    await nextTick
    scheduled = undefined
  }
  await $.agent.spawn({ subagentType: 'Explore', description: 'child-1', prompt: 'Inspect' })
  await $.agent.spawn({ subagentType: 'Explore', description: 'peer', prompt: 'Inspect another file' })
  expect(opened).toHaveLength(1)
  at = 4000
  await tick()
  expect(closed).toEqual([])
  const waiting = await $.ui.mount(pane)
  expect(await waiting.find({ type: 'Text', text: '2 running' })).toBeDefined()
  await waiting.unmount()
  status = 'completed'
  await tick()
  expect(closed).toEqual([])
  at = 10000
  const done = await $.ui.mount(pane)
  expect(await done.find({ type: 'Text', text: '1 running · 1 done' })).toBeDefined()
  expect(await done.find({ type: 'Text', text: '   → custom-model · 3s' })).toBeDefined()
  expect(await done.find({ type: 'Text', text: '✓ ' })).toMatchObject({ props: { color: 'green' } })
  expect(JSON.stringify(await done.drawn())).toContain('"color":"green"')
  await done.unmount()
  peerStatus = 'failed'
  await tick()
  expect(closed).toEqual([])
  const failed = await $.ui.mount(pane)
  expect(await failed.find({ type: 'Text', text: '0 running · 1 done · 1 failed' })).toBeDefined()
  expect(await failed.find({ type: 'Text', text: '● error ' })).toMatchObject({ props: { color: 'red' } })
  expect(await failed.find({ type: 'Text', text: '✓ ' })).toMatchObject({ props: { color: 'green' } })
  await failed.unmount()
  at = 14999
  await tick()
  expect(closed).toEqual([])
  await $.agent.spawn({ subagentType: 'Explore', description: 'child-2', prompt: 'Next batch' })
  at = 15000
  await tick()
  expect(closed).toEqual([])
  expect(opened).toHaveLength(1)
  const batch = await $.ui.mount(pane)
  expect(await batch.find({ type: 'Text', text: '1 running' })).toBeDefined()
  expect(JSON.stringify(await batch.drawn())).not.toContain('child-1')
  await batch.unmount()
  nextStatus = 'completed'
  at = 16000
  await tick()
  at = 20999
  await tick()
  expect(closed).toEqual([])
  await $.command.run({ command: 'subagents', args: '' })
  expect(opened[1]).toMatchObject({ columns: 23 })
  const lingering = await $.ui.mount(pane)
  expect(await lingering.find({ type: 'Text', text: '✓ 1 done' })).toBeDefined()
  await lingering.unmount()
  at = 21000
  await tick()
  expect(closed.map(event => event.id)).toEqual(['subagents', 'subagents'])
  expect(placed).toBe(false)
  at = 26000
  await tick()
  const prompt = { ...pane, component: 'AbovePrompt', requestId: 'subagent-summary' } as const
  const afterFinish = await $.ui.mount(prompt)
  expect(await afterFinish.find({ type: 'Text', text: /Subagents:|✓ 1 done|Expand/ })).toBeUndefined()
  expect(await afterFinish.find({ type: 'Text', text: 'native prompt' })).toBeDefined()
  await afterFinish.unmount()
  await tick()
  expect(closed).toHaveLength(2)
  await $.agent.spawn({ subagentType: 'Explore', description: 'child-3', prompt: 'Another batch' })
  expect(placed).toBe(true)
  const active = await $.ui.mount(prompt)
  expect(await active.find({ type: 'Text', text: /Subagents:/ })).toBeUndefined()
  await active.unmount()
  expect(opened).toHaveLength(3)
  expect(opened[2]).toMatchObject({ columns: 34 })
  expect(placed).toBe(true)
})
