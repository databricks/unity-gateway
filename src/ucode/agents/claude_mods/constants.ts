export const RECIPE_FIELD = 'smart_router_recipe_name';
export const DISABLED_RECIPE = 'DISABLED';
// Matches `routing.ROUTER_NAME` in Python: the recipe when a launch carried none of its own.
export const DEFAULT_RECIPE = 'stable';
export const CLI_MODULE = 'ucode.cli';
export const CLI_AGENT = 'claude';
export const ENABLE_ROUTING_FLAG = '--enable-smart-routing';
export const DISABLE_ROUTING_FLAG = '--disable-smart-routing';
// Omnigent matches the "Smart Router is on" and "Smart Router is off" replies exactly; update Omnigent if either changes.
export const SMART_ROUTER_ON_REPLY = 'Smart Router is on';
export const SMART_ROUTER_OFF_REPLY = 'Smart Router is off';
// Omnigent matches this warning exactly; update Omnigent if it changes.
export const UNSUPPORTED_MODEL_LIST_WARNING = "⚠ smart routing unsupported";
export const VALIDATION_FAILED_WARNING = "⚠ Smart Router couldn't validate this session's model list";
export const LOGS_DETAILS_HINT = '(/smart-router logs for details)';
export const UNSUPPORTED_ROUTER_STATE = `ERROR ${LOGS_DETAILS_HINT}`;
export const OMNI_LAUNCH_MODE = 'omni';
export const SMART_ROUTING_KEY = 'ENABLE_SMART_ROUTING_V2';
export const SUBAGENT_ONLY_KEY = 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY';
export const TRUE_ENV_VALUES: readonly string[] = ['1', 'true', 'yes', 'on'];

export function envBool(value: string | undefined): boolean {
  return TRUE_ENV_VALUES.includes((value ?? '').trim().toLowerCase());
}

export const MODEL_SET_PREFIX = '<local-command-stdout>Set model to ';
export const MODEL_KEPT_PREFIX = '<local-command-stdout>Kept model';
