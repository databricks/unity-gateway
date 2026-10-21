import type { EngineInterface, On } from 'claude-code';
import { envBool } from './constants.ts';

const FEEDBACK_PANE = 'smart-router-feedback';

type DeliveryReceipt = {
  feedback_id: string;
  delivery_status: string;
  [key: string]: unknown;
};

function readDeliveryReceipt(output: string): { receipt: DeliveryReceipt; line: string } | undefined {
  for (const line of output.trim().split('\n').reverse()) {
    try {
      const receipt = JSON.parse(line);
      if (typeof receipt?.feedback_id === 'string'
          && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(receipt.feedback_id)
          && typeof receipt.delivery_status === 'string') {
        return { receipt, line };
      }
    } catch {}
  }
  return undefined;
}

function showSending($: EngineInterface, feedbackId: string): void {
  $.ui.log(`Sending feedback in the background. You can keep working. Feedback ID: ${feedbackId}`);
}

function showSubmitted($: EngineInterface, feedbackId: string): void {
  $.ui.log(`Feedback submitted successfully. Feedback ID: ${feedbackId}`);
  $.ui.toast('Feedback submitted successfully ✅');
}

function showUnconfirmed($: EngineInterface, feedbackId: string): void {
  $.ui.log(`Feedback delivery is unconfirmed. Retry with /isaac-feedback retry ${feedbackId}.`);
}

function watchDelivery(
  $: EngineInterface,
  feedbackId: string,
  active: Set<string>,
  announce = true,
): void {
  if (active.has(feedbackId)) return;
  active.add(feedbackId);
  if (announce) showSending($, feedbackId);
  let attempts = 0;
  let checking = false;
  let timer: { cancel: () => void } | undefined;
  const stop = () => {
    timer?.cancel();
    active.delete(feedbackId);
  };
  const check = async () => {
    if (checking) return;
    checking = true;
    attempts += 1;
    try {
      const result = await $.process.run(['isaac', 'feedback', '--status', feedbackId], { timeoutMs: 10000 });
      const status = result.exitCode === 0 ? readDeliveryReceipt(result.stdout)?.receipt : undefined;
      if (status?.feedback_id !== feedbackId) throw new Error('Feedback status unavailable.');
      if (status.delivery_status === 'submitted') {
        stop();
        showSubmitted($, feedbackId);
      } else if (status.delivery_status !== 'queued') {
        throw new Error('Feedback needs a retry.');
      } else if (attempts >= 13) {
        stop();
        $.ui.log(`Feedback is still queued. Check it with /isaac-feedback status ${feedbackId}.`);
      }
    } catch {
      stop();
      showUnconfirmed($, feedbackId);
    } finally {
      checking = false;
    }
  };
  try {
    // Native clocks keep checking after the skill's turn ends and are disposed with the mod.
    timer = $.clock.every(5000, () => { void check(); });
    void check();
  } catch {
    stop();
    $.ui.log(`Check feedback delivery with /isaac-feedback status ${feedbackId}.`);
  }
}

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
  const active = new Set<string>();
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const answer = await next(e);
    // The skill still owns category/target selection and the original permission-checked call.
    if (typeof e.command !== 'string' || !e.command.includes('--background')
        || !/\bfeedback\b/.test(e.command)) return answer;
    try {
      const payload = answer.result;
      if (!payload || typeof payload !== 'object' || !('stdout' in payload)
          || typeof payload.stdout !== 'string') return answer;
      const parsed = readDeliveryReceipt(payload.stdout);
      if (!parsed || !['queued', 'submitted'].includes(parsed.receipt.delivery_status)
          || !envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))) return answer;
      watchDelivery($, parsed.receipt.feedback_id, active);
      // Tell the skill that native UI owns the acknowledgement, avoiding a duplicate model reply.
      const rendered = JSON.stringify({ ...parsed.receipt, delivery_notifications: 'native' });
      return {
        ...answer,
        result: { ...payload, stdout: payload.stdout.replace(parsed.line, rendered) },
        ...(typeof answer.text === 'string' ? { text: answer.text.replace(parsed.line, rendered) } : {}),
      };
    } catch {
      // A notification failure must not hide the submission result from the skill.
      return answer;
    }
  });

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
            showSending($, feedbackId);
            const background = state.backgroundSupported;
            void (async () => {
              const sessionId = await $.session.id();
              const args = ['isaac', 'feedback', '--category', 'smart-routing', '--harness', 'claude',
                '--session-id', sessionId, '--feedback-id', feedbackId];
              if (background) args.push('--background', '--target-before', targetBefore);
              const result = await $.process.run(args, { stdin: value, timeoutMs: 120000 });
              if (result.exitCode !== 0) throw new Error('Feedback command failed.');
              // Older Isaac releases finish delivery in the command itself.
              if (!background) {
                showSubmitted($, feedbackId);
                return;
              }
              const status = readDeliveryReceipt(result.stdout)?.receipt;
              if (status?.feedback_id !== feedbackId
                  || !['queued', 'submitted'].includes(status.delivery_status)) {
                throw new Error('Feedback needs a retry.');
              }
              watchDelivery($, feedbackId, active, false);
            })().catch(() => {
              // Keep the original ID even when the enqueue acknowledgement was lost.
              showUnconfirmed($, feedbackId);
            });
          },
        }),
        Button({ key: 'cancel', label: 'Cancel (keep routing on)', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
