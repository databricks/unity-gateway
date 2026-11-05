import type { EngineInterface, On, PluginOptions } from 'claude-code';

import { showStatusBand } from './smart-routing-status.ts';
import { setRouting, readEnvironment } from './routing-controls.ts';
import { requestAutoFeedback, type FeedbackState } from './isaac-feedback.ts';
import { isRoutingEnabled } from './routing-state.ts';

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

async function selectAuto($: EngineInterface, options: PluginOptions, policy: AutoPolicy, selected?: {text?: string}): Promise<{text: string}> {
  policy.picking = false;
  const model = options.autoMainModel;
  if (typeof model !== 'string' || !model.trim() || model.toLowerCase() === 'auto') {
    if (await $.session.model() === 'auto') policy.blocked = true;
    return {text: 'Auto needs a concrete default_model in the managed Claude config. Ask your admin to set it, then restart UG. Use /model M to continue with a concrete model.'};
  }
  policy.blocked = true;
  try {
    const result = selected ?? await $.command.run({command: 'model', args: model});
    // Interactive commands return a message reference; the session owns the selected model.
    if ((typeof result.text === 'string' && modelSelectionResult(result.text) !== 'selected') || await $.session.model() !== model) {
      return {text: 'Could not apply the admin default for Auto. Dispatch is paused; retry /model auto or choose /model M.'};
    }
    await complete($, {auto: true, args: []}, 'Set model to confirmed', options, policy);
    return {text: policy.blocked ? 'Could not enable Auto. Dispatch is paused; retry /model auto or choose /model M.' : `Auto enabled with admin default main model ${model}.`};
  } catch (error) {
    return {text: `Could not apply Auto: ${String(error)}. Dispatch is paused; retry /model auto or choose /model M.`};
  }
}

export function registerAutoPicker(
  on: On, options: PluginOptions, policy: AutoPolicy, feedback: FeedbackState,
): void {
  let pending: Choice | undefined;
  let interactive = true;
  let applyAuto = false;
  // Native redraws reuse the original command text, including our intentional save denial.
  const completedOutputs = new Map<string, string | undefined>();

  on('session.start', {isInteractive: false}, ($, e, next) => {
    interactive = e.isInteractive;
    return next(e);
  });

  on('command.run', {command: 'model'}, async ($, e, next) => {
    const args = e.args.trim();
    pending = undefined;
    applyAuto = false;
    policy.picking = false;
    if (!args && !interactive) return next(e);
    const auto = args.toLowerCase() === 'auto';
    if (auto) {
      const model = options.autoMainModel;
      if (typeof model !== 'string' || !model.trim() || model.toLowerCase() === 'auto') return selectAuto($, options, policy);
      policy.blocked = true;
      try {
        const result = await next({...e, args: model});
        return selectAuto($, options, policy, result);
      } catch (error) {
        return {text: `Could not apply Auto: ${String(error)}. Dispatch is paused; retry /model auto or choose /model M.`};
      }
    }
    if (args) {
      const handled = await leaveAuto($, options, feedback, async (feedbackArgs) => {
        pending = {auto: false, args: feedbackArgs};
        policy.picking = true;
        await $.command.run({command: 'model', args});
      });
      if (handled) return {};
    }
    pending = {auto, args: []};
    policy.picking = true;
    const result = await next(e);
    // Headless commands return text directly; interactive picker results arrive later.
    if (typeof result.text === 'string') {
      const choice = pending; pending = undefined;
      await complete($, choice, result.text, options, policy);
    }
    return result;
  });

  on('config.set', {key: 'model'}, async ($, e, next) => {
    if (e.value === 'auto') {
      pending = undefined;
      policy.blocked = true;
      applyAuto = true;
      return {deny: 'Applying Auto with the admin default main model.'};
    }
    if (pending && !pending.auto) {
      const model = String(e.value);
      const handled = await leaveAuto($, options, feedback, async (args) => {
        pending = {auto: false, args};
        const result = await $.command.run({command: 'model', args: model});
        const choice = pending; pending = undefined;
        if (typeof result.text === 'string') await complete($, choice, result.text, options, policy);
      });
      if (handled) { pending = undefined; return {deny: 'Complete Leave Auto feedback to change models.'}; }
    }
    const result = await next(e);
    if (pending && result.deny === undefined) {
      const choice = pending; pending = undefined;
      await complete($, choice, 'Set model to confirmed', options, policy);
    }
    return result;
  });

  on('ui.render', {component: 'CommandOutput'}, async ($, e, next) => {
    const fresh = !completedOutputs.has(e.requestId);
    if (!fresh) {
      const text = completedOutputs.get(e.requestId);
      return next(text === undefined ? e : {...e, props: {...e.props, text}});
    }
    completedOutputs.set(e.requestId, undefined);
    if (/^\/?model$/.test(String(e.props.command))) {
      // Native session-only picks bypass config.set.
      if (await $.session.model() === 'auto') {
        pending = undefined;
        const result = await selectAuto($, options, policy);
        completedOutputs.set(e.requestId, result.text);
        return next({...e, props: {...e.props, text: result.text}});
      }
      if (applyAuto) {
        applyAuto = false;
        policy.blocked = true;
        $.clock.after(1, async () => {
          const result = await selectAuto($, options, policy);
          completedOutputs.set(e.requestId, result.text);
          $.ui.invalidate('ui.render');
        });
        const text = 'Applying Auto with the admin default main model.';
        completedOutputs.set(e.requestId, text);
        return next({...e, props: {...e.props, text}});
      }
      const choice = pending; pending = undefined;
      await complete($, choice, e.props.text, options, policy);
    }
    return next(e);
  });

}
