export const RECIPE_FIELD = 'smart_router_recipe_name';
export const DISABLED_RECIPE = 'DISABLED';
export const CLI_MODULE = 'ucode.cli';
export const CLI_AGENT = 'claude';
export const ENABLE_ROUTING_FLAG = '--enable-smart-routing';
export const DISABLE_ROUTING_FLAG = '--disable-smart-routing';
// The CLI's exit code when it refuses an off's reason or feedback ID.
export const CLI_REJECTED_EXIT_CODE = 2;
export const ROUTING_UPDATE_FAILED_REPLY = 'Could not update Smart Router. Check the recipe, CLAUDE_CODE_EXTRA_BODY, and environment permissions; retry or restart through UG.';
export const SMART_ROUTING_KEY = 'ENABLE_SMART_ROUTING_V2';
export const SUBAGENT_ONLY_KEY = 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY';
export const TRUE_ENV_VALUES: readonly string[] = ['1', 'true', 'yes', 'on'];

export function envBool(value: string | undefined): boolean {
  return TRUE_ENV_VALUES.includes((value ?? '').trim().toLowerCase());
}
