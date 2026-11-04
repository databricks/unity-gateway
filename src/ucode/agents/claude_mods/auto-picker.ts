import type { EngineInterface, On, PluginOptions } from 'claude-code';

import { setRouting, readEnvironment } from './routing-controls.ts';
import { requestAutoFeedback, type FeedbackState } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

const PANE = 'ug-model-picker';

type Choice = { auto: boolean; args: readonly string[] };
export type AutoPolicy = { revision: number; blocked: boolean };

// Claude 2.1.317 completes both saved and session-only picks through local_command.
// Cancellation has a distinct "Kept model as" result, including same-model picks.
export function modelSelectionResult(text: string): 'selected' | 'cancelled' | undefined {
  const clean = text.replace(/\x1b\[[0-9;]*m/g, '').trim();
  if (clean.startsWith('Set model to ')) return 'selected';
  if (clean.startsWith('Kept model as ')) return 'cancelled';
}

async function complete($: EngineInterface, choice: Choice | undefined, text: string, options: PluginOptions, policy: AutoPolicy) {
    if (!choice) return;
    if (modelSelectionResult(text) !== 'selected') return;
    // A failed write must never allow inference with a partially applied policy.
    policy.blocked = true;
    policy.revision += 1;
    try {
      await setRouting($, options, choice.auto, true, choice.args);
      policy.blocked = false;
      $.ui.invalidate('ui.render');
    } catch (error) {
      $.ui.log(`Could not apply the model's Auto policy: ${String(error)}. Dispatch is paused; retry /model or restart through UG.`);
    }
}


async function leaveAuto($: EngineInterface, options: PluginOptions, feedback: FeedbackState, proceed: (args: readonly string[]) => Promise<void>): Promise<boolean> {
  if (!isRoutingEnabled(await readEnvironment($))) return false;
  return requestAutoFeedback($, feedback, options.isaacExecutable as string, options.isaacFeedbackDirectory as string, proceed);
}

export function registerAutoPicker(
  on: On, options: PluginOptions, policy: AutoPolicy, feedback: FeedbackState,
): void {
  let pending: Choice | undefined;
  let fromMenu: Choice | undefined;
  let interactive = true;
  const completedOutputs = new Set<string>();

  on('session.start', {isInteractive: false}, ($, e, next) => {
    interactive = e.isInteractive;
    return next(e);
  });

  on('command.run', {command: 'model'}, async ($, e, next) => {
    const args = e.args.trim();
    const menuChoice = fromMenu;
    fromMenu = undefined;
    pending = undefined;
    if (!args && !menuChoice) {
      if (!interactive) return {text: 'Choose a concrete model with /model M; Auto requires the Claude terminal.'};
      const pane = await $.ui.open({id: PANE, title: 'Choose model mode', focus: true, closeOnEscape: true, rows: 5, columns: 64});
      if (!pane.isPlaced) return {text: 'Could not open /model. Retry in the Claude terminal.'};
      return {};
    }
    const auto = menuChoice?.auto ?? args.toLowerCase() === 'auto';
    if (auto && !interactive) return {text: 'Use /model auto in the Claude terminal to confirm a concrete main model.'};
    if (!auto && !menuChoice) {
      const handled = await leaveAuto($, options, feedback, async (feedbackArgs) => {
        fromMenu = {auto: false, args: feedbackArgs};
        await $.command.run({command: 'model', args});
      });
      if (handled) return {};
    }
    pending = menuChoice ?? {auto, args: []};
    const result = await next({...e, args: auto || menuChoice && !args ? '' : args});
    // Headless commands return text directly; interactive picker results arrive later.
    if (typeof result.text === 'string') {
      const choice = pending; pending = undefined;
      await complete($, choice, result.text, options, policy);
    }
    return result;
  });

  on('config.set', {key: 'model'}, async ($, e, next) => {
    const result = await next(e);
    if (pending && result.deny === undefined) {
      const choice = pending; pending = undefined;
      await complete($, choice, 'Set model to confirmed', options, policy);
    }
    return result;
  });

  on('ui.render', {component: 'CommandOutput'}, async ($, e, next) => {
    const fresh = !completedOutputs.has(e.requestId);
    completedOutputs.add(e.requestId);
    if (pending && fresh && /^\/?model$/.test(String(e.props.command))) {
      const choice = pending; pending = undefined;
      await complete($, choice, e.props.text, options, policy);
    }
    return next(e);
  });

  on('ui.render', {component: 'Pane'}, ($, e, next) => {
    if (e.requestId !== PANE) return next(e);
    const {Box, Text, Button} = $.ui.resolve(e);
    const choose = async (auto: boolean) => {
      await $.ui.close({id: PANE});
      const open = async (args: readonly string[]) => {
        fromMenu = {auto, args};
        await $.command.run({command: 'model', args: ''});
      };
      if (!auto && await leaveAuto($, options, feedback, open)) return;
      await open([]);
    };
    return Box({flexDirection: 'column', children: [
      Text({children: ['Choose a mode, then confirm your main model in Claude’s picker.']}),
      Button({key: 'auto', label: 'Auto — choose a main model; automatically route subagents', autoFocus: true, onPress: () => choose(true)}),
      Button({key: 'concrete', label: 'Concrete model — turn Auto off', onPress: () => choose(false)}),
      Button({key: 'cancel', label: 'Cancel', onPress: () => $.ui.close({id: PANE})}),
    ]});
  });
}
