import type { EngineInterface, On } from 'claude-code';
import { DEFAULT_RECIPE, DISABLED_RECIPE, RECIPE_FIELD } from './constants.ts';
import { mergeExtraBody } from './extra-body.ts';
import { isRoutingEnabled } from './routing-state.ts';

// Writes the launch's smart-router recipe into `CLAUDE_CODE_EXTRA_BODY`, which Claude's security
// default reserves to policy-tier mods and denies to the user-tier session mod. ug installs it
// beside ug-headers as the organization's mod and lists it with it in the managed prepend tier.
// A `/smart-router` toggle lands in the environment during its command, and this mod re-reads
// the flags on every turn step, so the next request carries it whatever the order of the mods.
// Only the recipe field is managed: every other request field is preserved, and a session ug did
// not route is left untouched.

async function routingFlags($: EngineInterface): Promise<Record<string, unknown> | null> {
  // Claude's capability scanner requires literal environment-variable names.
  const environment: Record<string, unknown> = {
    ENABLE_SMART_ROUTING_V2: await $.env.get('ENABLE_SMART_ROUTING_V2'),
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY'),
  };
  // No routing flags in the environment: not a ug-routed launch, so the body is not ours.
  if (!Object.values(environment).some((value) => typeof value === 'string')) return null;
  const sessionFile = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (sessionFile) {
    try {
      const controls = JSON.parse(await $.fs.read(sessionFile));
      for (const key of Object.keys(environment)) {
        if (typeof controls?.[key] === 'string') environment[key] = controls[key];
      }
    } catch {
      // Match the routing hooks' inherited-environment fallback for unreadable controls.
    }
  }
  return environment;
}

async function apply($: EngineInterface): Promise<void> {
  const flags = await routingFlags($);
  if (flags === null) return;
  const current = await $.env.get('CLAUDE_CODE_EXTRA_BODY');
  let recipe: string;
  if (isRoutingEnabled(flags)) {
    const configured = await $.env.get('SMART_ROUTER_NAME');
    recipe = typeof configured === 'string' && configured.trim() ? configured : DEFAULT_RECIPE;
  } else {
    // Only replace a recipe that is already there: a session that never routed keeps its body.
    try {
      const body = JSON.parse(current ?? '{}');
      if (!body || typeof body !== 'object' || Array.isArray(body) || !(RECIPE_FIELD in body)) {
        return;
      }
    } catch {
      return;
    }
    recipe = DISABLED_RECIPE;
  }
  let merged: string;
  try {
    merged = mergeExtraBody(current, {[RECIPE_FIELD]: recipe});
  } catch {
    return; // A caller body that is not a JSON object is left alone.
  }
  if (merged !== current) await $.env.set('CLAUDE_CODE_EXTRA_BODY', merged);
}

export function register(on: On): void {
  on('session.start', async ($, e, next) => {
    await apply($);
    return next(e);
  });
  // Claude rebuilds its client per request and a settings reload resets the variable, so add the
  // recipe again before each one.
  on('turn.step', async function* ($, e, next) {
    await apply($);
    return yield* next(e);
  });
}
