import type { On } from 'claude-code';

const FEEDBACK_PANE = 'smart-router-feedback';
const TOGGLE_ERROR = 'Could not update Smart Router. Retry /smart-router off or restart through UG.';

type FeedbackState = {
  submitted: boolean;
  reason: string;
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: () => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

export function registerIsaacFeedback(on: On, state: FeedbackState): void {
  on('command.run', {command: 'smart-router'}, async ($, e, next) => {
    if (e.args.trim() !== 'off' || await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK') !== '1') {
      return next(e);
    }
    try {
      if (!state.readRoutingEnabled || !state.disableRouting) {
        throw new Error('Smart Router controls are unavailable.');
      }
      if (!await state.readRoutingEnabled()) return {text: 'Smart Router is off'};
      // Start the toggle before opening feedback; dismissing the pane must not cancel it.
      void state.disableRouting().then(
        () => $.ui.log('Smart Router is off'),
        () => $.ui.log(TOGGLE_ERROR),
      );
    } catch {
      return {text: TOGGLE_ERROR};
    }
    try {
      state.submitted = false;
      state.reason = '';
      const pane = await $.ui.open({
        id: FEEDBACK_PANE, title: 'Smart Router feedback', focus: true,
        closeOnEscape: true, rows: 6, columns: 56,
      });
      if (pane.isPlaced) return {text: 'Turning Smart Router off in the background.'};
      // Do not leave a delayed question waiting for an unrelated task.
      await $.ui.close({ id: FEEDBACK_PANE });
    } catch {
      // Feedback availability does not affect the toggle already running.
    }
    return {text: 'Turning Smart Router off in the background. Could not open the feedback form.'};
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
          submitLabel: 'Send feedback',
          onInput: (value) => { state.reason = value; },
          onSubmit: async (value) => {
            if (state.submitted) return;
            if (Array.from(value.trim()).length < 16) {
              $.ui.toast('Enter at least 16 characters, excluding surrounding whitespace.');
              return;
            }
            state.submitted = true;
            await $.ui.close({ id: FEEDBACK_PANE }).catch(() => {});
            // Upload in the background through Isaac's CLI, without a model turn.
            const feedbackId = crypto.randomUUID();
            $.ui.log(`Sending feedback in the background. You can keep working. Feedback ID: ${feedbackId}`);
            void $.session.id()
              .then((sessionId) => $.process.run(
                ['isaac', 'feedback', '--category', 'smart-routing', '--harness', 'claude',
                  '--session-id', sessionId, '--feedback-id', feedbackId],
                { stdin: value, timeoutMs: 30000 },
              ))
              .then((result) => result.exitCode === 0, () => false)
              // A failure goes to the transcript, where it outlasts a toast.
              .then((sent) => sent
                ? $.ui.toast('Feedback submitted successfully ✅')
                : $.ui.log('Feedback could not be sent. Retry with /isaac-feedback --category smart-routing.'));
          },
        }),
        Button({ key: 'cancel', label: 'Cancel', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
