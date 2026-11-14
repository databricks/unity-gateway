export const config = {
  model: 'ug-auto-opus', description: 'Auto description', mainModel: 'opus', storeKey: 'workspace-main',
  models: [
    {model: 'opus', label: 'Opus 5.5', autoModel: 'ug-auto-opus'}, {model: 'fable-old', label: 'Fable 5.0', autoModel: 'ug-auto-fable-old'},
    {model: 'fable', label: 'Fable 5.1', autoModel: 'ug-auto-fable'}, {model: 'astra', label: 'Astra 6.0', autoModel: 'ug-auto-astra'},
  ],
};
export const options = {
  ENABLE_SMART_ROUTING_V2: '0', ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '0', ENABLE_SMART_ROUTER_AUTO_MODE: '1', unset: [], recipe: 'task_v3',
  autoPicker: JSON.stringify(config),
};
export const start = {surface: 'terminal', isInteractive: true, cwd: '/work'} as const;
