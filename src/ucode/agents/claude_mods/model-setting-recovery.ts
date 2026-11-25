import type { EngineInterface, On, PluginOptions } from 'claude-code';

async function receipt(
  $: EngineInterface,
  command: readonly string[],
  warning: {shown: boolean},
  event: Record<string, unknown>,
): Promise<void> {
  try {
    const result = await $.process.run(command, {stdin: JSON.stringify(event), timeoutMs: 10000});
    if (result.exitCode !== 0 || JSON.parse(result.stdout).acknowledged !== true) {
      throw new Error('Model-setting receipt was not acknowledged');
    }
  } catch {
    if (!warning.shown) $.ui.log('Smart Routing could not track this model change. Check your saved default.');
    warning.shown = true;
  }
}

export function registerModelSettingRecovery(on: On, options: PluginOptions): void {
  const command = options.modelSettingCommand as readonly string[] | undefined;
  if (!command?.length) return;
  let sequence = 0;
  let activeCommand: number | undefined;
  const warning = {shown: false};
  on('command.run', {command: ['model', 'config', 'settings', 'fast', 'auto']}, async ($, e, next) => {
    const id = ++sequence;
    activeCommand = id;
    try {
      if (e.command === 'model') {
        await receipt($, command, warning, {event: 'command', id, command: e.command, origin: e.origin.kind});
      }
      return await next(e);
    } finally {
      if (activeCommand === id) activeCommand = undefined;
    }
  });
  on('config.set', {key: 'model'}, async ($, e, next) => {
    const id = ++sequence;
    // Capture this before awaiting: another command can start before the write answers.
    const commandId = e.origin.kind === 'plugin' ? undefined : activeCommand;
    await receipt($, command, warning, {event: 'prepare', id, command_id: commandId});
    try {
      const result = await next(e);
      if (result.deny !== undefined || typeof result.value !== 'string') {
        await receipt($, command, warning, {event: 'failed', id});
      } else {
        // config.set runs only for saved defaults; picker session-only choices bypass it.
        await receipt($, command, warning, {event: 'saved', id, model: result.value === 'default' ? null : result.value});
      }
      return result;
    } catch (error) {
      await receipt($, command, warning, {event: 'failed', id});
      throw error;
    }
  });
}
