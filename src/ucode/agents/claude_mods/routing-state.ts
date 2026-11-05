import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY } from './constants.ts';

export function isRoutingEnabled(environment: Readonly<Record<string, unknown>>): boolean {
  return environment[SMART_ROUTING_KEY] === '1' || environment[SUBAGENT_ONLY_KEY] === '1';
}
