export const RECIPE_FIELD = 'smart_router_recipe_name';
export const DISABLED_RECIPE = 'DISABLED';
// Luna Fast sessions ask the gateway for its fast speed through this body field.
export const SPEED_FIELD = 'speed';
export const FAST_SPEED = 'fast';
export const CLI_MODULE = 'ucode.cli';
export const CLI_AGENT = 'claude';
export const ENABLE_ROUTING_FLAG = '--enable-smart-routing';
export const DISABLE_ROUTING_FLAG = '--disable-smart-routing';
export const SMART_ROUTING_KEY = 'ENABLE_SMART_ROUTING_V2';
export const SUBAGENT_ONLY_KEY = 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY';
export const TRUE_ENV_VALUES: readonly string[] = ['1', 'true', 'yes', 'on'];

export function envBool(value: string | undefined): boolean {
  return TRUE_ENV_VALUES.includes((value ?? '').trim().toLowerCase());
}
