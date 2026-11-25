import type { EngineInterface, On, PluginOptions } from 'claude-code';

export function registerModelSettingRecovery(on: On, options: PluginOptions): void {
  const command = options.modelSettingCommand as readonly string[] | undefined;
  if (!command?.length) return;
  let sequence = 0;
  let activeCommand: number | undefined;
  let warned = false;
  async function receipt($: EngineInterface, event: Record<string, unknown>): Promise<void> {
    try {
      const result = await $.process.run(command!, {stdin: JSON.stringify(event), timeoutMs: 10000});
      if (result.exitCode !== 0 || JSON.parse(result.stdout).acknowledged !== true) {
        throw new Error('Model-setting receipt was not acknowledged');
      }
    } catch {
      if (!warned) $.ui.log('Smart Routing could not track this model change. Check your saved default.');
      warned = true;
    }
  }
  on('command.run', {command: ['model', 'config', 'settings', 'fast', 'auto']}, async ($, e, next) => {
    const id = ++sequence;
    activeCommand = id;
    await receipt($, {event: 'command', id, command: e.command, origin: e.origin.kind});
    return next(e);
  });
  on('config.set', {key: 'model'}, async ($, e, next) => {
    const id = ++sequence;
    // Capture this before awaiting: another command can start before the write answers.
    const commandId = e.origin.kind === 'plugin' ? undefined : activeCommand;
    await receipt($, {event: 'prepare', id, command_id: commandId});
    try {
      const result = await next(e);
      if (result.deny !== undefined || typeof result.value !== 'string') {
        await receipt($, {event: 'failed', id});
      } else {
        // config.set runs only for saved defaults; picker session-only choices bypass it.
        await receipt($, {event: 'saved', id, model: result.value === 'default' ? null : result.value});
      }
      return result;
    } catch (error) {
      await receipt($, {event: 'failed', id});
      throw error;
    }
  });
}
