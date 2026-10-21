import type { EngineInterface, On } from 'claude-code';
import { envBool } from './constants.ts';

const FEEDBACK_PANE = 'smart-router-feedback';
const FEEDBACK_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
type FeedbackCategory = 'smart-routing' | 'other';

function feedbackSuffix(feedbackId: string): string {
  return feedbackId ? ` Feedback ID: ${feedbackId}` : '';
}

function showSending($: EngineInterface, feedbackId: string): void {
  $.ui.log(`Sending feedback in the background. You can keep working.${feedbackSuffix(feedbackId)}`);
}

function showResult($: EngineInterface, feedbackId: string, sent: boolean): void {
  if (sent) {
    $.ui.log(`Feedback submitted successfully.${feedbackSuffix(feedbackId)}`);
    $.ui.toast('Feedback submitted successfully ✅');
  } else {
    const retry = feedbackId ? `/isaac-feedback retry ${feedbackId}` : '/isaac-feedback';
    $.ui.log(`Feedback delivery is unconfirmed. Retry with ${retry}.`);
  }
}

type FeedbackState = {
  submitted: boolean;
  reason: string;
  category?: FeedbackCategory;
  turnOff?: boolean;
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: (sessionControlArgs: readonly string[]) => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

function uploadFeedback(
  $: EngineInterface, isaacExecutable: string, feedbackId: string, message?: string,
): void {
  showSending($, feedbackId);
  const args = feedbackId ? ['--retry', feedbackId] : ['--category', 'other'];
  void $.process.run([isaacExecutable, 'feedback', ...args], { stdin: message, timeoutMs: 120000 })
    .then(result => showResult($, feedbackId, result.exitCode === 0),
      () => showResult($, feedbackId, false));
}

async function submitFeedback(
  $: EngineInterface, state: FeedbackState, isaacExecutable: string,
  isaacFeedbackDirectory: string, message: string,
): Promise<string | undefined> {
  if (state.submitted) return;
  const minimum = state.turnOff ? 16 : 1;
  if (Array.from(message.trim()).length < minimum) {
    return state.turnOff
      ? 'Enter at least 16 characters, excluding surrounding whitespace.'
      : 'Enter feedback before sending.';
  }
  if (!state.category || !isaacExecutable || !isaacFeedbackDirectory) return 'Isaac feedback is unavailable.';
  state.submitted = true;
  $.ui.invalidate('ui.render');
  const feedbackId = state.category === 'smart-routing' ? crypto.randomUUID() : '';
  try {
    if (feedbackId) {
      const capture = await $.process.run(
        [isaacExecutable, 'feedback', '--category', 'smart-routing', '--harness', 'claude',
          '--session-id', await $.session.id(), '--feedback-id', feedbackId, '--with-evidence',
          '--capture-local', isaacFeedbackDirectory],
        { stdin: message, timeoutMs: 120000 },
      );
      if (capture.exitCode !== 0) throw new Error('Could not save feedback.');
      if (capture.stderr.trim()) $.ui.log(capture.stderr.trim());
    }
    if (state.turnOff) {
      if (!state.disableRouting) throw new Error('Smart Router controls are unavailable.');
      await state.disableRouting(['--smart-router-feedback', message]);
    }
  } catch {
    state.submitted = false;
    $.ui.invalidate('ui.render');
    return state.turnOff
      ? 'Could not save feedback or update Smart Router. Smart Router is still on; retry or restart through UG.'
      : 'Could not save feedback. Nothing was uploaded; retry /isaac-feedback.';
  }
  await $.ui.close({ id: FEEDBACK_PANE }).catch(() => {});
  state.submitted = false;
  uploadFeedback($, isaacExecutable, feedbackId, feedbackId ? undefined : message);
}

async function openFeedbackForm(
  $: EngineInterface, state: FeedbackState, isaacExecutable: string, isaacFeedbackDirectory: string,
): Promise<boolean> {
  if (!isaacExecutable || !isaacFeedbackDirectory) return false;
  // `isaac --version` launches a harness; the help command checks the installed CLI.
  const help = await $.process.run([isaacExecutable, 'feedback', '--help'], { timeoutMs: 10000 });
  if (help.exitCode !== 0) return false;
  $.ui.invalidate('ui.render');
  const pane = await $.ui.open({
    id: FEEDBACK_PANE, title: state.turnOff ? 'Turn Smart Router off' : 'Isaac feedback',
    focus: true, closeOnEscape: true, rows: 6, columns: 56,
  });
  if (pane.isPlaced) return true;
  await $.ui.close({ id: FEEDBACK_PANE });
  return false;
}

export function registerIsaacFeedback(
  on: On, state: FeedbackState, isaacExecutable: string, isaacFeedbackDirectory: string,
): void {
  on('command.run', { command: 'isaac-feedback' }, async ($, e, next) => {
    if (!envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))) return next(e);
    if (state.submitted) return { text: 'Feedback is still being saved.' };
    let message = e.args;
    let category: FeedbackCategory | undefined;
    if (message.startsWith('-- ')) {
      message = message.slice(3);
    } else {
      for (const value of ['smart-routing', 'other'] as const) {
        const prefix = `--category ${value}`;
        if (message === prefix || message.startsWith(prefix + ' ')) {
          category = value;
          message = message.slice(prefix.length + 1);
          break;
        }
      }
      if (!category) {
        const [action, feedbackId, ...rest] = message.trim().split(/\s+/);
        if (action === 'retry' || action === '--retry') {
          if (!isaacExecutable) return { text: 'Isaac feedback is unavailable.' };
          if (!feedbackId || rest.length || !FEEDBACK_ID.test(feedbackId)) {
            return { text: 'Usage: /isaac-feedback retry <feedback-id>' };
          }
          uploadFeedback($, isaacExecutable, feedbackId);
          return {};
        }
        // Keep explicit inspection and local-preview requests with the existing skill.
        if (message.startsWith('--')
            || (action === 'status' && feedbackId && !rest.length && FEEDBACK_ID.test(feedbackId))) return next(e);
      }
    }
    state.turnOff = false;
    state.category = category;
    state.reason = message;
    state.submitted = false;
    if (category && message.trim()) {
      const error = await submitFeedback($, state, isaacExecutable, isaacFeedbackDirectory, message);
      return error ? { text: error } : {};
    }
    try {
      if (await openFeedbackForm($, state, isaacExecutable, isaacFeedbackDirectory)) return {};
    } catch {
      // An unavailable form must not submit feedback or leave a delayed question.
    }
    return { text: 'Could not open the feedback form. Retry /isaac-feedback in the Claude terminal.' };
  });

  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    if (e.args.trim() !== 'off') return next(e);
    const feedbackRequired = envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'));
    if (!feedbackRequired) return next(e);
    if (state.submitted) return { text: 'Feedback is still being saved.' };
    try {
      if (!state.readRoutingEnabled || !state.disableRouting) {
        throw new Error('Smart Router controls are unavailable.');
      }
      if (!await state.readRoutingEnabled()) return {text: 'Smart Router is off'};
      state.submitted = false;
      state.reason = '';
      state.category = 'smart-routing';
      state.turnOff = true;
      if (await openFeedbackForm($, state, isaacExecutable, isaacFeedbackDirectory)) return {text: 'Smart Router is off'};
    } catch {
      // No reason was collected, so leave routing unchanged.
    }
    return {text: 'Could not open the feedback form. Smart Router is still on; retry /smart-router off.'};
  });

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== FEEDBACK_PANE || e.surface === 'mobile') return next(e);
    const { Box, Text, Input, Button } = $.ui.resolve(e);
    if (state.submitted) return Text({ children: ['Saving feedback...'] });
    if (!state.category) {
      const choose = async (category: FeedbackCategory) => {
        state.category = category;
        if (state.reason.trim()) {
          const error = await submitFeedback($, state, isaacExecutable, isaacFeedbackDirectory, state.reason);
          if (error) $.ui.toast(error);
        }
        $.ui.invalidate('ui.render');
      };
      return Box({ flexDirection: 'column', children: [
        Text({ children: ['What is your feedback about?'] }),
        Button({ key: 'routing', label: 'Smart routing (with session details)', autoFocus: true, onPress: () => choose('smart-routing') }),
        Button({ key: 'other', label: 'Other (text only)', onPress: () => choose('other') }),
        Button({ key: 'cancel', label: 'Cancel', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ] });
    }
    return Box({
      flexDirection: 'column',
      children: [
        Text({ children: [state.turnOff ? 'Why are you turning Smart Router off?' : 'What would you like to share?'] }),
        Text({ children: [state.turnOff
          ? 'Enter at least 16 characters. Your reason is sent to the Isaac team with this session\'s details.'
          : state.category === 'smart-routing'
            ? 'Your feedback is sent to the Isaac team with this session\'s details.'
            : 'Your feedback is sent to the Isaac team without session evidence.'] }),
        Input({
          key: 'reason',
          label: state.turnOff ? 'Reason' : 'Feedback',
          value: state.reason,
          autoFocus: true,
          submitLabel: state.turnOff ? 'Send feedback and turn off' : 'Send feedback',
          onInput: (value) => { state.reason = value; },
          onSubmit: async (value) => {
            const error = await submitFeedback($, state, isaacExecutable, isaacFeedbackDirectory, value);
            if (error) $.ui.toast(error);
          },
        }),
        Button({ key: 'cancel', label: state.turnOff ? 'Cancel (keep routing on)' : 'Cancel', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
