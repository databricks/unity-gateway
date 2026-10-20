import { SMART_ROUTING_KEY, SUBAGENT_ONLY_KEY, TRUE_ENV_VALUES } from './constants.ts';

export function envBool(value: string | undefined): boolean {
  return TRUE_ENV_VALUES.includes((value ?? '').trim().toLowerCase());
}

export function isRoutingEnabled(environment: Readonly<Record<string, unknown>>): boolean {
  return environment[SMART_ROUTING_KEY] === '1' || environment[SUBAGENT_ONLY_KEY] === '1';
}
