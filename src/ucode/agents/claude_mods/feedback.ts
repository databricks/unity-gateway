import type { On } from 'claude-code';

export const FEEDBACK_PANE = 'smart-router-feedback';

type FeedbackState = { offered: boolean; submitted: boolean; reason: string };

export function registerFeedback(on: On, state: FeedbackState): void {
  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== FEEDBACK_PANE || e.surface === 'mobile') return next(e);
    const { Box, Text, Input, Button } = $.ui.resolve(e);
    return Box({
      flexDirection: 'column',
      children: [
        Text({ children: ['Why are you turning Smart Router off? (optional)'] }),
        Text({ children: ['Send your reason and available session evidence with /isaac-feedback.'] }),
        Input({
          key: 'reason',
          label: 'Reason',
          value: state.reason,
          autoFocus: true,
          submitLabel: 'Send feedback',
          onInput: (value) => { state.reason = value; },
          onSubmit: async (value) => {
            if (state.submitted || !value.trim()) return;
            state.submitted = true;
            try {
              await $.ui.close({ id: FEEDBACK_PANE });
              await $.command.run({ command: 'isaac-feedback', args: `--category smart-routing ${value}` });
            } catch {
              $.ui.toast('Could not open /isaac-feedback. Smart Router is still off.');
            }
          },
        }),
        Button({ key: 'skip', label: 'Skip', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
