// Values the mod shares with UG's CLI and the gateway.
// Environment-variable names are deliberately absent: Claude's capability scanner
// requires a literal name at each `$.env.get`/`$.env.set` call site so it can list
// what the module reads and writes, and `plugin validate` rejects a variable there.
export const RECIPE_HEADER = 'Databricks-Smart-Router-Recipe';
export const DISABLED_RECIPE = 'DISABLED';
export const CLI_MODULE = 'ucode.cli';
export const CLI_AGENT = 'claude';
export const ENABLE_ROUTING_FLAG = '--enable-smart-routing';
export const DISABLE_ROUTING_FLAG = '--disable-smart-routing';
