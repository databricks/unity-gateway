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
  lastPromptId?: string | null;
  category?: FeedbackCategory;
  turnOff?: boolean;
  readRoutingEnabled?: () => Promise<boolean>;
  disableRouting?: (sessionControlArgs: readonly string[]) => Promise<void>;
};

export function createIsaacFeedbackState(): FeedbackState {
  return { submitted: false, reason: '' };
}

async function feedbackEnvironment($: EngineInterface): Promise<Record<string, string>> {
  const env: Record<string, string> = {
    CLAUDE_CODE_SESSION_ID: await $.session.id(),
    // A nested Claude launch can inherit another harness's identity markers.
    CODEX_THREAD_ID: '', CODEX_CI: '', CODEX_HOME: '', CODEX_SANDBOX: '', OPENCODE: '', CURSOR_AGENT: '',
    ENABLE_SMART_ROUTING_V2: (await $.env.get('ENABLE_SMART_ROUTING_V2')) === '1' ? '1' : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: (await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY')) === '1' ? '1' : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: (await $.env.get('ENABLE_SMART_ROUTER_ORCHESTRATOR')) ?? '',
    SMART_ROUTER_NAME: (await $.env.get('SMART_ROUTER_NAME')) ?? '',
    SMART_ROUTER_CONFIG_VERSION: (await $.env.get('SMART_ROUTER_CONFIG_VERSION')) ?? '',
    UCODE_SESSION_ENV_FILE: '',
  };
  const controls = await $.env.get('UCODE_SESSION_ENV_FILE');
  if (controls) {
    try {
      const overrides = JSON.parse(await $.fs.read(controls));
      const keys = ['ENABLE_SMART_ROUTING_V2', 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY', 'ENABLE_SMART_ROUTER_ORCHESTRATOR'];
      if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)
          || Object.entries(overrides).some(([key, value]) => !keys.includes(key) || (value !== '0' && value !== '1'))) {
        throw new Error('Invalid routing controls.');
      }
      Object.assign(env, overrides);
    } catch {
      // Let Isaac report unreadable controls instead of claiming a verified state.
      env.UCODE_SESSION_ENV_FILE = controls;
    }
  }
  // Valid controls are frozen here; a later toggle must not change this report.
  return env;
}

async function prepareFeedbackTarget(
  $: EngineInterface, state: FeedbackState, isaacExecutable: string,
): Promise<void> {
  if (state.lastPromptId !== undefined) return;
  // Resumed sessions may not have emitted a prompt during this plugin's lifetime.
  const result = await $.process.run(
    [isaacExecutable, 'feedback', '--inspect-target', '--harness', 'claude', '--session-id', await $.session.id()],
    { timeoutMs: 10000 },
  );
  if (result.exitCode !== 0) throw new Error('Could not identify the feedback target.');
  if (state.lastPromptId === undefined) state.lastPromptId = JSON.parse(result.stdout).message_id ?? null;
}

async function uploadFeedback(
  $: EngineInterface, isaacExecutable: string, isaacFeedbackDirectory: string, feedbackId: string,
  args: string[], message?: string, env?: Record<string, string>,
): Promise<void> {
  try {
    const result = await $.process.run([isaacExecutable, 'feedback', ...args], { stdin: message, env, timeoutMs: 120000 });
    if (result.exitCode === 0) {
      if (result.stderr.trim()) $.ui.log(result.stderr.trim());
      showResult($, feedbackId, true);
      return;
    }
  } catch {
    // A timeout can leave a saved report, which should be retried with its original ID.
  }
  const saved = feedbackId && await $.fs.exists(`${isaacFeedbackDirectory}/${feedbackId}/receipt.json`).catch(() => false);
  showResult($, saved ? feedbackId : '', false);
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
  const feedbackId = state.category === 'smart-routing' ? crypto.randomUUID() : '';
  const { turnOff, disableRouting } = state;
  let env: Record<string, string> | undefined;
  let promptId: string | null | undefined;
  let args = ['--category', 'other'];
  try {
    if (feedbackId) {
      await prepareFeedbackTarget($, state, isaacExecutable);
      promptId = state.lastPromptId;
      env = await feedbackEnvironment($);
      args = ['--category', 'smart-routing', '--harness', 'claude',
        '--session-id', env.CLAUDE_CODE_SESSION_ID, '--feedback-id', feedbackId, '--with-evidence',
        // An unmatched selector keeps an empty session from picking up the user's next task.
        '--turn-id', promptId ?? 'no-prior-task'];
    }
  } catch {
    state.submitted = false;
    return 'Could not prepare feedback. Nothing was uploaded; retry /isaac-feedback.';
  }
  await $.ui.close({ id: FEEDBACK_PANE }).catch(() => {});
  showSending($, feedbackId);
  if (feedbackId && promptId === null) {
    $.ui.log('No earlier task was found. This feedback can help diagnose setup or controls, but has no task or model request to analyze.');
  }
  void uploadFeedback($, isaacExecutable, isaacFeedbackDirectory, feedbackId, args, message, env);
  if (turnOff) {
    try {
      if (!disableRouting) throw new Error('Smart Router controls are unavailable.');
      await disableRouting(['--smart-router-feedback', message]);
    } catch {
      $.ui.log('Could not turn Smart Router off. Feedback is still being sent; retry /smart-router off or restart through UG.');
    }
  }
  state.submitted = false;
}

async function openFeedbackForm(
  $: EngineInterface, state: FeedbackState, isaacExecutable: string, isaacFeedbackDirectory: string,
): Promise<boolean> {
  if (!isaacExecutable || !isaacFeedbackDirectory) return false;
  if (state.category !== 'other') await prepareFeedbackTarget($, state, isaacExecutable);
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
  on('session.append', { door: 'prompt' }, ($, e, next) => {
    if (!e.agentId && !e.message.isMeta) state.lastPromptId = e.uuid;
    return next(e);
  });
  on('session.end', ($, e, next) => {
    state.lastPromptId = undefined;
    return next(e);
  });
  on('command.run', { command: 'isaac-feedback' }, async ($, e, next) => {
    if (!envBool(await $.env.get('ISAAC_SMART_ROUTER_FEEDBACK'))) return next(e);
    if (state.submitted) return { text: 'Feedback is being prepared.' };
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
          showSending($, feedbackId);
          void uploadFeedback($, isaacExecutable, isaacFeedbackDirectory, feedbackId, ['--retry', feedbackId]);
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
    if (state.submitted) return { text: 'Feedback is being prepared.' };
    try {
      if (!state.readRoutingEnabled || !state.disableRouting) {
        throw new Error('Smart Router controls are unavailable.');
      }
      if (!await state.readRoutingEnabled()) return {text: 'Smart Router is off'};
      state.submitted = false;
      state.reason = '';
      state.category = 'smart-routing';
      state.turnOff = true;
      if (await openFeedbackForm($, state, isaacExecutable, isaacFeedbackDirectory)) return {};
    } catch {
      // No reason was collected, so leave routing unchanged.
    }
    return {text: 'Could not open the feedback form. Smart Router is still on; retry /smart-router off.'};
  });

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== FEEDBACK_PANE || e.surface === 'mobile') return next(e);
    const { Box, Text, Input, Button } = $.ui.resolve(e);
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
