import type { EngineInterface, On, PluginOptions } from 'claude-code';

import { showStatusBand } from './smart-routing-status.ts';
import { setRouting, readEnvironment } from './routing-controls.ts';
import { requestAutoFeedback, type FeedbackState } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

const PANE = 'ug-model-picker';

type Choice = { auto: boolean; args: readonly string[] };
export type AutoPolicy = { revision: number; blocked: boolean; picking: boolean };

// Native cancellation and headless selection text on Claude 2.1.317.
// Interactive saved selections use config.set, including same-model picks.
export function modelSelectionResult(text: string): 'selected' | 'cancelled' | undefined {
  const clean = text.replace(/\x1b\[[0-9;]*m/g, '').trim();
  if (clean.startsWith('Set model to ')) return 'selected';
  if (clean.startsWith('Kept model as ')) return 'cancelled';
}

async function complete($: EngineInterface, choice: Choice | undefined, text: string, options: PluginOptions, policy: AutoPolicy) {
    if (!choice) return;
    policy.picking = false;
    if (modelSelectionResult(text) !== 'selected') return;
    // A failed write must never allow inference with a partially applied policy.
    policy.blocked = true;
    policy.revision += 1;
    try {
      await setRouting($, options, choice.auto, true, choice.args);
      policy.blocked = false;
      showStatusBand();
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
  let interactive = true;
  const completedOutputs = new Set<string>();

  on('session.start', {isInteractive: false}, ($, e, next) => {
    interactive = e.isInteractive;
    return next(e);
  });

  on('command.run', {command: 'model'}, async ($, e, next) => {
    const args = e.args.trim();
    pending = undefined;
    policy.picking = false;
    if (!args) {
      if (!interactive) return {text: 'Choose a concrete model with /model M; Auto requires the Claude terminal.'};
      const pane = await $.ui.open({id: PANE, title: 'Choose model mode', focus: true, closeOnEscape: true, rows: 5, columns: 64});
      if (!pane.isPlaced) return {text: 'Could not open /model. Retry in the Claude terminal.'};
      return {};
    }
    const auto = args.toLowerCase() === 'auto';
    if (auto && !interactive) return {text: 'Use /model auto in the Claude terminal to confirm a concrete main model.'};
    if (!auto) {
      const handled = await leaveAuto($, options, feedback, async (feedbackArgs) => {
        pending = {auto: false, args: feedbackArgs};
        policy.picking = true;
        await $.command.run({command: 'model', args});
      });
      if (handled) return {};
    }
    pending = {auto, args: []};
    policy.picking = true;
    const result = await next({...e, args: auto ? '' : args});
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
        // Calls from this mod skip its own command handler.
        pending = {auto, args};
        policy.picking = true;
        await $.command.run({command: 'model', args: ''});
      };
      if (!auto && await leaveAuto($, options, feedback, open)) return;
      await open([]);
    };
    return Box({flexDirection: 'column', children: [
      Text({children: ['Choose a mode, then confirm your main model with Enter.']}),
      Button({key: 'auto', label: 'Auto — choose a main model; automatically route subagents', autoFocus: true, onPress: () => choose(true)}),
      Button({key: 'concrete', label: 'Concrete model — turn Auto off', onPress: () => choose(false)}),
      Button({key: 'cancel', label: 'Cancel', onPress: () => $.ui.close({id: PANE})}),
    ]});
  });
}
