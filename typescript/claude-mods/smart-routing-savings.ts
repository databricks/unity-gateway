import type { Register } from 'claude-code'

type On = Parameters<Register>[0]

// The savings concern of ug's smart-routing UI mod (composed by register.ts).
// It only collects: per turn request (main agent and subagents) it sums token
// usage by (agent, baseline, served), where served is the model that answered and
// baseline is what the request would have used without routing: the main model in
// effect for a subagent, the model that answered for main (routing never changes
// the main model mid-session; first-prompt routing is priced from start_model).
// Claude Code's side queries (title, compaction, helpers) are not turn steps, so
// they never count. A hooks module has no Node APIs and the price table lives in
// Python, so after each turn it writes the sums next to UCODE_SESSION_ENV_FILE,
// runs the pricer command ug put in UCODE_SAVINGS_PRICER (a JSON argv), and keeps
// the strings it prints for smart-routing-status.ts to draw. Everything is best
// effort and swallows errors.
// The env var and file names are kept in sync with ucode.smart_routing.claude_statusline
// (a test guards it).

const USAGE_FILE = 'mod-usage.json'
const PRICER_TIMEOUT_MS = 20_000
const TOKEN_KEYS = [
  'input_tokens',
  'output_tokens',
  'cache_creation_input_tokens',
  'cache_read_input_tokens',
] as const
const CACHE_SPLIT_KEYS = ['ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'] as const

type Totals = Record<(typeof TOKEN_KEYS)[number], number> & {
  cache_creation?: Partial<Record<(typeof CACHE_SPLIT_KEYS)[number], number>>
}
type Entry = { agent: string; baseline: string; served: string; usage: Totals }
type Priced = { savings: string | null; plugin: string | null }

// Module state is shared with smart-routing-status.ts through savingsSegments().
const entries = new Map<string, Entry>()
// The model the session started on, before any first-prompt routing. Set once.
let startModel: string | null = null
// The model the main loop last named, so a subagent's baseline is a real model id
// ($.session.model() may return an alias).
let lastMainModel: string | null = null
let seeding: Promise<void> | undefined
let pricer: string[] | null | undefined
let priced: Priced = { savings: null, plugin: null }
let dirty = false
let running = false
let rerun = false

const num = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : 0

const str = (value: unknown): string | null => (typeof value === 'string' && value ? value : null)

// A hooks module has no Node APIs: env and files come through the mods API, and
// $.env.get must be called with a string literal. The pricer argv is only read
// once; absent or malformed means the feature is off.
async function readPricer($: any): Promise<string[] | null> {
  if (pricer === undefined) {
    let found: string[] | null = null
    try {
      const parsed = JSON.parse(await $.env.get('UCODE_SAVINGS_PRICER'))
      if (Array.isArray(parsed) && parsed.length > 0 && parsed.every(arg => typeof arg === 'string')) {
        found = parsed
      }
    } catch {}
    pricer = found
  }
  return pricer
}

// Same directory derivation as subagent-routing.ts: the per-session dir ug's
// Python side writes next to UCODE_SESSION_ENV_FILE.
async function usagePath($: any): Promise<string | null> {
  let file: unknown
  try {
    file = await $.env.get('UCODE_SESSION_ENV_FILE')
  } catch {}
  if (typeof file !== 'string') return null
  const cut = Math.max(file.lastIndexOf('/'), file.lastIndexOf('\\'))
  return cut > 0 ? `${file.slice(0, cut)}/${USAGE_FILE}` : null
}

async function mainModel($: any): Promise<string | null> {
  try {
    return str(await $.session.model())
  } catch {
    return null
  }
}

function accumulate(agent: string, baseline: string, served: string, usage: any): void {
  const key = JSON.stringify([agent, baseline, served])
  let entry = entries.get(key)
  if (!entry) {
    entry = {
      agent,
      baseline,
      served,
      usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    }
    entries.set(key, entry)
  }
  for (const k of TOKEN_KEYS) entry.usage[k] += num(usage[k])
  // The 5m/1h cache-write split is only summed where the API reported it.
  const split = usage.cache_creation
  if (split && typeof split === 'object') {
    const sums = entry.usage.cache_creation ?? {}
    for (const k of CACHE_SPLIT_KEYS) if (k in split) sums[k] = (sums[k] ?? 0) + num(split[k])
    entry.usage.cache_creation = sums
  }
}

// A mod reload resets module state while the session carries on, so the first
// hook of a fresh module instance picks up where the last one left off from the
// file it wrote. A missing or malformed file just means starting empty.
async function loadUsage($: any): Promise<void> {
  try {
    const path = await usagePath($)
    if (!path) return
    const saved = JSON.parse(await $.fs.read(path))
    if (!saved || saved.version !== 1 || !Array.isArray(saved.entries)) return
    for (const item of saved.entries) {
      const agent = str(item?.agent)
      const baseline = str(item?.baseline)
      const served = str(item?.served)
      if (agent && baseline && served && item.usage && typeof item.usage === 'object') {
        accumulate(agent, baseline, served, item.usage)
        dirty = true
      }
    }
    startModel ??= str(saved.start_model)
    lastMainModel ??= str(saved.main_model)
  } catch {}
}

// Whether the feature is on, with state seeded first (once per module instance,
// whichever hook runs first; concurrent hooks wait on the same load).
async function enable($: any): Promise<boolean> {
  if (!(await readPricer($))) return false
  seeding ??= loadUsage($)
  await seeding
  return true
}

// What the pricer printed on its last line. A failed run drops the estimate (it
// would be stale) but keeps the plugin version, which does not depend on usage.
async function runPricer($: any, argv: string[], path: string): Promise<Priced> {
  try {
    const run = await $.process.run([...argv, '--mod-usage', path], { timeoutMs: PRICER_TIMEOUT_MS })
    if (run?.exitCode === 0) {
      const out = JSON.parse(String(run.stdout ?? '').trim().split('\n').pop() ?? '')
      if (out && typeof out === 'object') return { savings: str(out.savings), plugin: str(out.plugin) }
    }
  } catch {}
  return { savings: null, plugin: priced.plugin }
}

async function writeAndPrice($: any): Promise<void> {
  const argv = await readPricer($)
  const path = await usagePath($)
  if (!argv || !path) return
  dirty = false
  try {
    await $.fs.write(
      path,
      JSON.stringify({
        version: 1,
        start_model: startModel,
        main_model: lastMainModel,
        entries: Array.from(entries.values()),
      }),
    )
  } catch (error) {
    // Retry at the next turn.complete rather than pricing a file that is not there.
    dirty = true
    throw error
  }
  const next = await runPricer($, argv, path)
  if (next.savings !== priced.savings || next.plugin !== priced.plugin) {
    priced = next
    // Claude Code only redraws the band when its props change, not when module state does.
    $.ui.invalidate('ui.render')
  }
}

// One pricer run at a time; a request that arrives mid-run just asks for another.
async function flush($: any): Promise<void> {
  if (running) {
    rerun = true
    return
  }
  running = true
  try {
    do {
      rerun = false
      await writeAndPrice($)
    } while (rerun)
  } catch {
  } finally {
    running = false
  }
}

// Strings for smart-routing-status.ts to append after its trailer, in order;
// empty until the pricer has something to show (or when the feature is off).
export const savingsSegments = (): string[] =>
  [priced.savings, priced.plugin].filter((segment): segment is string => segment !== null)

export const registerSavings = (on: On): void => {
  // This concern owns session.start for the composed module (a second
  // unmatched hook on the same event fails the module load). It runs before the
  // first prompt, so the start model is captured before any routing happens and
  // the plugin version can show at once. session.start can fire again (/clear),
  // by when first-prompt routing may have switched the model, so the start model
  // is only ever set once. The flush is not awaited: it spawns a process and
  // Claude Code waits on this hook before the first prompt.
  on('session.start', async ($, e, next) => {
    if (await enable($)) {
      const model = await mainModel($)
      startModel ??= model
      void flush($)
    }
    return next(e)
  })

  // Fires for every request to a model, subagents included (e.agentId is set).
  // yield* forwards the streamed response untouched; only the finished result's
  // usage (which names the model that answered) is read.
  on('turn.step', async function* ($, e, next) {
    const request = e as any
    const agent = str(request.agentId)
    const enabled = await enable($)
    let baseline: string | null = null
    if (enabled) {
      if (agent === null) lastMainModel = str(request.model) ?? lastMainModel
      else baseline = lastMainModel ?? (await mainModel($))
    }
    const result = yield* next(e)
    try {
      const usage = (result as any)?.usage
      const served = str(usage?.model) ?? str(request.model)
      // Main's own baseline is what answered it: routing leaves the main model alone.
      const base = agent === null ? served : baseline
      if (enabled && usage && served && base) {
        accumulate(agent ?? 'main', base, served, usage)
        dirty = true
      }
    } catch {}
    return result
  })

  // Price at the end of each turn, off the turn's critical path.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if ((await enable($)) && dirty) void flush($)
    return result
  })
}
