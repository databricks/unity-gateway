import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts';

export function isRoutingEnabled(environment: Readonly<Record<string, unknown>>): boolean {
  return environment[SMART_ROUTING_KEY] === '1' || environment[SUBAGENT_ONLY_KEY] === '1';
}

// Parses the on-demand file a session that started with Smart Router off keeps.
export function neverTurnedOn(onDemandJson: string): boolean {
  try {
    return JSON.parse(onDemandJson)?.ever_on === false;
  } catch {
    return false;
  }
}
