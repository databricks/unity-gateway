import type { On } from 'claude-code';
import { envBool } from './constants.ts';

const FEEDBACK_PANE = 'smart-router-feedback';

type FeedbackState = {
  submitted: boolean;
  reason: string;
  backgroundSupported?: boolean;
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: (sessionControlArgs: readonly string[]) => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

export function registerIsaacFeedback(on: On, state: FeedbackState): void {
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    if (e.args.trim() !== 'off') return next(e);
    const feedbackRequired = envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'));
    if (!feedbackRequired) return next(e);
    try {
      if (!state.readRoutingEnabled || !state.disableRouting) {
        throw new Error('Smart Router controls are unavailable.');
      }
      if (!await state.readRoutingEnabled()) return {text: 'Smart Router is off'};
      // The form uploads through Isaac's CLI; without it, keep routing on. `isaac --version` launches a harness.
      const help = await $.process.run(['isaac', 'feedback', '--help'], { timeoutMs: 10000 });
      if (help.exitCode !== 0) {
        throw new Error('Isaac CLI is unavailable.');
      }
      state.backgroundSupported = ['--background', '--status', '--target-before'].every((flag) => help.stdout.includes(flag));
      state.submitted = false;
      state.reason = '';
      const pane = await $.ui.open({
        id: FEEDBACK_PANE, title: 'Turn Smart Router off', focus: true,
        closeOnEscape: true, rows: 6, columns: 56,
      });
      if (pane.isPlaced) return {text: 'Smart Router is off'};
      // Do not leave a delayed question waiting for an unrelated task.
      await $.ui.close({ id: FEEDBACK_PANE });
    } catch {
      // No reason was collected, so leave routing unchanged.
    }
    return {text: 'Could not open the feedback form. Smart Router is still on; retry /smart-router off.'};
  });

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== FEEDBACK_PANE || e.surface === 'mobile') return next(e);
    const { Box, Text, Input, Button } = $.ui.resolve(e);
    return Box({
      flexDirection: 'column',
      children: [
        Text({ children: ['Why are you turning Smart Router off?'] }),
        Text({ children: ['Enter at least 16 characters. Your reason is sent to the Isaac team with this session\'s details.'] }),
        Input({
          key: 'reason',
          label: 'Reason',
          value: state.reason,
          autoFocus: true,
          submitLabel: 'Send feedback and turn off',
          onInput: (value) => { state.reason = value; },
          onSubmit: async (value) => {
            if (state.submitted || !state.disableRouting) return;
            if (Array.from(value.trim()).length < 16) {
              $.ui.toast('Enter at least 16 characters, excluding surrounding whitespace.');
              return;
            }
            state.submitted = true;
            const targetBefore = new Date().toISOString();
            try {
              await state.disableRouting(['--smart-router-feedback', value]);
            } catch {
              state.submitted = false;
              $.ui.toast('Could not update Smart Router. Smart Router is still on; retry or restart through UG.');
              return;
            }
            await $.ui.close({ id: FEEDBACK_PANE }).catch(() => {});
            // Upload in the background through Isaac's CLI, without a model turn.
            const feedbackId = crypto.randomUUID();
            $.ui.log(`Sending feedback in the background. You can keep working. Feedback ID: ${feedbackId}`);
            const background = state.backgroundSupported;
            void (async () => {
              const sessionId = await $.session.id();
              const args = ['isaac', 'feedback', '--category', 'smart-routing', '--harness', 'claude',
                '--session-id', sessionId, '--feedback-id', feedbackId];
              if (background) args.push('--background', '--target-before', targetBefore);
              let result = await $.process.run(args, { stdin: value, timeoutMs: 120000 });
              if (result.exitCode !== 0) throw new Error('Feedback command failed.');
              // Older Isaac releases finish delivery in the command itself.
              if (!background) {
                $.ui.toast('Feedback submitted successfully ✅');
                return;
              }
              for (let attempt = 0; attempt <= 12; attempt += 1) {
                const status = JSON.parse(result.stdout);
                if (status.delivery_status === 'submitted') {
                  $.ui.toast('Feedback submitted successfully ✅');
                  return;
                }
                if (status.delivery_status !== 'queued') throw new Error('Feedback needs a retry.');
                if (attempt === 12) break;
                await new Promise((resolve) => setTimeout(resolve, 5000));
                result = await $.process.run(['isaac', 'feedback', '--status', feedbackId], { timeoutMs: 10000 });
                if (result.exitCode !== 0) throw new Error('Feedback status unavailable.');
              }
              $.ui.log(`Feedback is still queued. Check it with /isaac-feedback status ${feedbackId}.`);
            })().catch(() => {
              // Keep the original ID even when the enqueue acknowledgement was lost.
              $.ui.log(`Smart Router is off. Feedback delivery is unconfirmed. Retry with /isaac-feedback retry ${feedbackId}.`);
            });
          },
        }),
        Button({ key: 'cancel', label: 'Cancel (keep routing on)', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
