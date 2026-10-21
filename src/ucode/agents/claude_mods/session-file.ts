export type SessionSwitch = { enabled: boolean; whenOn: Record<string, string>; everEnabled: boolean }

// UG's CLI writes the session file; the mod only reads it. Claude's capability scanner follows
// $ only into functions declared in the same file, so each module reads the file itself and
// shares just this parser. Undefined means the file is unreadable or in an earlier format.
export function parseSessionSwitch(raw: string): SessionSwitch | undefined {
  try {
    const controls = JSON.parse(raw)
    if (
      typeof controls?.enabled === 'boolean' &&
      typeof controls.ever_enabled === 'boolean' &&
      controls.when_on !== null &&
      typeof controls.when_on === 'object'
    ) {
      return { enabled: controls.enabled, whenOn: controls.when_on, everEnabled: controls.ever_enabled }
    }
  } catch {}
  return undefined
}
