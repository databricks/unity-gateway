import type { EngineInterface, On } from 'claude-code';
import { envBool } from './constants.ts';

const FEEDBACK_PANE = 'smart-router-feedback';
const FEEDBACK_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

function literalCommand(command: string): { raw: string[]; args: string[] } | undefined {
  // The skill emits one literal command. Leave shell expansion and compound commands alone.
  const word = /(?:[^\s'"\\$`;&|<>()*?[\]{}#~]+|'[^']*'|"[^"\\$`]*"|\\[^\r\n])+/g;
  const raw = command.match(word);
  if (!raw || !/^[ \t]*$/.test(command.replace(word, ''))) return;
  const args = raw.map(value => value.replace(/'([^']*)'|"([^"]*)"|\\(.)/g,
    (_, single, double, escaped) => single ?? double ?? escaped));
  if (!/(?:^|[/\\])isaac(?:\.exe)?$/.test(args[0]) || args[1] !== 'feedback') return;
  return { raw, args };
}

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
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: (sessionControlArgs: readonly string[]) => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

export function registerIsaacFeedback(on: On, state: FeedbackState, isaacExecutable: string): void {
  const uploads = new Map<string, string>();
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (typeof e.command !== 'string' || !e.command.includes('feedback')) return next(e);
    const command = literalCommand(e.command);
    if (!command || !envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))) return next(e);
    const { raw, args } = command;
    const delimiter = args.indexOf('--');
    const options = delimiter < 0 ? args.slice(2) : args.slice(2, delimiter);
    if (options.some(value => ['--help', '-h', '--inspect-target', '--capture-local', '--retry-pending'].includes(value.split('=')[0]))) {
      return next(e);
    }
    const option = (name: string): string | undefined => {
      const index = options.findIndex(value => value === name || value.startsWith(`${name}=`));
      if (index < 0) return;
      return options[index] === name ? options[index + 1] : options[index].slice(name.length + 1);
    };
    let feedbackId = option('--retry') || '';
    if (options.some(value => value.split('=')[0] === '--retry') && !FEEDBACK_ID.test(feedbackId)) return next(e);
    const category = option('--category') || option('-c') || 'other';
    const evidence = options.includes('--with-evidence') || category === 'smart-routing';
    let captureNotice = '';
    let answer;
    if (!feedbackId && evidence) {
      const captureOptions = ['--capture-local', "'~/.isaac/feedback'"];
      const captureCommand = delimiter < 0 ? [...raw, ...captureOptions]
        : [...raw.slice(0, delimiter), ...captureOptions, ...raw.slice(delimiter)];
      const capture = await next({ ...e, command: captureCommand.join(' '), run_in_background: false });
      const payload = capture.result;
      if (capture.isError || capture.deny || !payload || typeof payload !== 'object'
          || !('stdout' in payload) || typeof payload.stdout !== 'string') return capture;
      const id = /[/\\]([0-9a-f-]{36})[/\\]receipt\.json/i.exec(payload.stdout)?.[1];
      if (!id || !FEEDBACK_ID.test(id)) return capture;
      feedbackId = id;
      captureNotice = payload.stdout.split('Local feedback receipt:')[0].trim();
      answer = await $.tool.call({
        tool: 'Bash', tool_use_id: `${e.tool_use_id}-upload`,
        command: `${raw[0]} feedback --retry ${id}`,
        description: 'Upload saved Isaac feedback', run_in_background: true,
      });
    } else {
      if (!feedbackId && category !== 'other') return next(e);
      answer = await next({ ...e, run_in_background: true });
    }
    // Native Bash owns permission checks, process lifetime, and the completion event.
    const payload = answer.result;
    if (answer.isError || answer.deny || !payload || typeof payload !== 'object'
        || !('backgroundTaskId' in payload) || typeof payload.backgroundTaskId !== 'string') return answer;
    uploads.set(payload.backgroundTaskId, feedbackId);
    showSending($, feedbackId);
    const text = JSON.stringify({
      feedback_id: feedbackId || null,
      delivery_notifications: 'native',
      capture_notice: captureNotice || undefined,
      instruction: 'The native UI reports sending and completion. Relay capture_notice if present, then end without repeating these messages, polling, or calling TaskOutput.',
    });
    return { ...answer, text, result: { ...payload, stdout: text } };
  });

  on('prompt.submit', { origin: { kind: 'task-notification' } }, ($, e, next) => {
    const taskId = /<task-id>([^<]+)<\/task-id>/.exec(e.text)?.[1];
    if (!taskId || !uploads.has(taskId)) return next(e);
    const feedbackId = uploads.get(taskId)!;
    uploads.delete(taskId);
    showResult($, feedbackId, e.text.includes('<status>completed</status>'));
    return { drop: 'Feedback completion reported by the native UI.' };
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
      if (!isaacExecutable) throw new Error('Isaac CLI is unavailable.');
      // The form uploads through Isaac's CLI; without it, keep routing on. `isaac --version` launches a harness.
      const help = await $.process.run([isaacExecutable, 'feedback', '--help'], { timeoutMs: 10000 });
      if (help.exitCode !== 0) {
        throw new Error('Isaac CLI is unavailable.');
      }
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
    if (state.submitted) return Text({ children: ['Saving feedback...'] });
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
            $.ui.invalidate('ui.render');
            const feedbackId = crypto.randomUUID();
            try {
              const sessionId = await $.session.id();
              const capture = await $.process.run(
                [isaacExecutable, 'feedback', '--category', 'smart-routing', '--harness', 'claude',
                  '--session-id', sessionId, '--feedback-id', feedbackId, '--with-evidence',
                  '--capture-local', '~/.isaac/feedback'],
                { stdin: value, timeoutMs: 120000 },
              );
              if (capture.exitCode !== 0) throw new Error('Could not save feedback.');
              if (capture.stderr.trim()) $.ui.log(capture.stderr.trim());
              await state.disableRouting(['--smart-router-feedback', value]);
            } catch {
              state.submitted = false;
              $.ui.invalidate('ui.render');
              $.ui.toast('Could not save feedback or update Smart Router. Smart Router is still on; retry or restart through UG.');
              return;
            }
            await $.ui.close({ id: FEEDBACK_PANE }).catch(() => {});
            // Upload in the background through Isaac's CLI, without a model turn.
            showSending($, feedbackId);
            void $.process.run([isaacExecutable, 'feedback', '--retry', feedbackId], { timeoutMs: 120000 })
              .then(result => showResult($, feedbackId, result.exitCode === 0),
                () => showResult($, feedbackId, false));
          },
        }),
        Button({ key: 'cancel', label: 'Cancel (keep routing on)', onPress: () => $.ui.close({ id: FEEDBACK_PANE }) }),
      ],
    });
  });
}
