import type { On, PluginOptions } from 'claude-code'

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

export const shortModel = (model: string): string =>
  model.replace(/^system\.ai\./, '').replace(/^databricks-/, '')

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

// Nothing left to clear; kept for the register module's shared turn.complete handler.
export function endSubagentRoutingTurn(_aborted: boolean): void {}

export const registerSubagentRouting = (on: On, options: PluginOptions): void => {
  const names = new Map<string, string>()
  const unusedNames = [...ANIMALS]
  // Calls waiting on the router, by when routing started, shown with a spinner and timer.
  const pending = new Map<string, number | undefined>()
  // The routed model by call; the decision itself lives in the /smart-router logs pane.
  const models = new Map<string, string>()

  on('tool.call', { tool: ['Agent', 'Task'] }, async ($, e, next) => {
    const command = options.routeCommand as readonly string[]
    if (!command.length || e.subagent_type === 'fork' || e.resume) return next(e)
    const { tool, tool_use_id, agentId, ...input } = e
    let route
    let spinner: { cancel: () => void } | undefined
    if (tool_use_id) {
      if (!names.has(tool_use_id)) {
        if (!unusedNames.length) unusedNames.push(...ANIMALS)
        const animal = unusedNames.splice(Math.floor(Math.random() * unusedNames.length), 1)[0]
        const cycle = Math.floor(names.size / ANIMALS.length)
        names.set(tool_use_id, cycle ? `${animal} ${cycle + 1}` : animal)
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
        stdin: JSON.stringify({ session_id, tool_use_id, tool_name: tool, tool_input: input }),
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
    if (tool_use_id) {
      pending.delete(tool_use_id)
      if (route) models.set(tool_use_id, route.model)
      $.ui.invalidate('ui.render')
    }
    // Auto mode reviewed this input; choose the child's model at agent.spawn instead.
    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    const model = e.resume ? undefined : models.get(e.tool_use_id)
    const name = model && names.get(e.tool_use_id)
    const result = await next(model ? { ...e, model } : e)
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

  // While routing waits the row grows a spinner line; once it completes the row renders
  // as the original Agent row with its animal name, with no decision drawn inline.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const name = names.get(e.requestId)
    if (!name || (!models.has(e.requestId) && !pending.has(e.requestId))) return next(e)
    const original = await next({ ...e, props: { ...e.props, tool: name } })
    if (models.has(e.requestId)) return original
    const { Box, Text } = $.ui.resolve(e)
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
}
