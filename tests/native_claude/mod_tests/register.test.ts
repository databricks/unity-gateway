import type { AgentInfo, On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import { RECIPE_FIELD } from '../hooks/constants.ts';
import { mergeExtraBody } from '../hooks/extra-body.ts';
import { resolveAutoModel, sameAutoTarget } from '../hooks/auto-model.ts';
import { agentTree, activityMark, activityText, safePreviewText } from '../hooks/agent-tree-state.ts';
import { NATIVE_VIEW } from '../hooks/agent-view.ts';

const RECIPE = 'task_v3';
const CALLER_BODY = '{"caller_field":"keep"}';
const ROUTED = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  unset: [],
  recipe: RECIPE,
};
const start = { surface: 'terminal', isInteractive: true, cwd: '/work' } as const;

// A pinned environment variable is the policy refusal the managed security default
// raises; only the extra body may carry the recipe.
function setup(on: On, { body = CALLER_BODY, pinned = '', launched = true } = {}) {
  // A routed launch puts its flags in the environment; session.start reads them back
  // so a reload keeps the toggle the session last applied.
  const env: Record<string, string | undefined> = {
    CLAUDE_CODE_EXTRA_BODY: body,
    ENABLE_SMART_ROUTING_V2: launched ? ROUTED.ENABLE_SMART_ROUTING_V2 : '0',
    ENABLE_SMART_ROUTING_SUBAGENT_ONLY: launched ? ROUTED.ENABLE_SMART_ROUTING_SUBAGENT_ONLY : '0',
    ENABLE_SMART_ROUTER_ORCHESTRATOR: launched ? ROUTED.ENABLE_SMART_ROUTER_ORCHESTRATOR : '0',
  };
  const writes: string[] = [];
  on('env.get', ($, e) => ({ value: env[e.name] }));
  on('env.set', ($, e) => {
    writes.push(e.name);
    if (e.name === pinned) return { deny: `env (managed): ${e.name} is not for plugins outside policy to change` };
    env[e.name] = e.value;
    return { value: undefined };
  });
  return { env, writes };
}

test('session start carries the launch recipe and forwards the event and result', { options: ROUTED }, async ($, on) => {
  const { env, writes } = setup(on);
  let calls = 0;
  on('session.start', ($, e) => {
    calls += 1;
    expect(e).toMatchObject(start);
    return { cwd: '/resolved-work' };
  });

  const result = await $.session.start(start);
  expect(calls).toBe(1);
  expect(result).toEqual({ cwd: '/resolved-work' });
  expect(JSON.parse(env.CLAUDE_CODE_EXTRA_BODY!)).toEqual({ caller_field: 'keep', [RECIPE_FIELD]: RECIPE });
  // The recipe left ANTHROPIC_CUSTOM_HEADERS, which a managed config pins.
  expect(writes).not.toContain('ANTHROPIC_CUSTOM_HEADERS');
});

test('an Auto flag without a launch picker leaves model and effort commands native', {
  options: { ...ROUTED, ENABLE_SMART_ROUTER_AUTO_MODE: '1' },
}, async ($, on) => {
  const { env } = setup(on);
  env.ENABLE_SMART_ROUTER_AUTO_MODE = '1';
  const registered: string[] = [];
  const commands: { command: string; args: string }[] = [];
  on('session.start', () => ({ cwd: '/work' }));
  on('command.register', ($, e) => {
    registered.push(e.name);
    return { value: undefined };
  });
  on('command.run', ($, e) => {
    commands.push({ command: e.command, args: e.args });
    return { ref: 1, text: `native ${e.command} ${e.args}` };
  });
  on('ui.invalidate', () => ({ value: undefined }));
  await $.session.start(start);

  expect((await $.command.run({ command: 'model', args: 'sonnet' })).text).toBe('native model sonnet');
  expect((await $.command.run({ command: 'effort', args: 'high' })).text).toBe('native effort high');
  expect(commands).toEqual([{ command: 'model', args: 'sonnet' }, { command: 'effort', args: 'high' }]);
  expect(registered).not.toContain('auto');
});

test('a denied extra-body write restores the environment and reports failure', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on, { pinned: 'CLAUDE_CODE_EXTRA_BODY' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
  // The rollback puts the flags the launch set back, so routing stays as it was.
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
});

test('a caller body that is not a JSON object is left alone', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on, { body: 'not json' });

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('Could not update Smart Router');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe('not json');
});

test('a launch without routing keeps the command inert', { options: { ...ROUTED, ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '0' } }, async ($, on) => {
  const { env } = setup(on, { launched: false });
  on('session.start', () => ({ cwd: '/work' }));
  await $.session.start(start);

  const reply = await $.command.run({ command: 'smart-router', args: 'off' });

  expect(reply.text).toContain('was not enabled');
  expect(env.CLAUDE_CODE_EXTRA_BODY).toBe(CALLER_BODY);
});

test('merging keeps other request metadata and rejects a non-object body', async () => {
  expect(JSON.parse(mergeExtraBody('{"caller":1,"label":"old"}', { label: 'new', nested: { enabled: true } })))
    .toEqual({ caller: 1, label: 'new', nested: { enabled: true } });
  expect(JSON.parse(mergeExtraBody(undefined, { value: 42 }))).toEqual({ value: 42 });
  for (const raw of ['null', '[]', 'true', '1', '"text"', 'broken']) {
    expect(() => mergeExtraBody(raw, {})).toThrow();
  }
});

const ABOVE_PROMPT = { plugin: 'ug-smart-router', surface: 'terminal', component: 'AbovePrompt', props: {} } as const;

const hasBand = (tree: unknown): boolean => JSON.stringify(tree).includes('unity gateway smart router');

// The engine's own process, drawing, prompt, and turn answers sit beneath the mod, and
// session controls are available so toggles succeed.
type MotionPreference = boolean | undefined;

function engine(
  on: On,
  env: Record<string, string | undefined>,
  agents: AgentInfo[] = [],
  onAgentList?: () => void,
  readMotion: () => MotionPreference = () => true,
) {
  const clock = mock.clock(on);
  const views: unknown[] = [];
  const focusEvents: unknown[] = [];
  env.UCODE_SMART_ROUTER_PYTHON = '/ug/python';
  env.UCODE_SESSION_ENV_FILE = '/session/env.json';
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }));
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('ui.render', { component: 'Pane' }, ($, e) => $.ui.resolve(e).Text({ children: ['native pane'] }));
  on('settings.read', () => {
    const preference = readMotion();
    return { value: preference === undefined ? {} : { prefersReducedMotion: preference } };
  });
  on('config.list', () => ({ value: [{ key: 'theme', value: 'dark' }] }));
  on('ui.panes', () => ({ value: [] }));
  on('ui.focus', ($, e) => { focusEvents.push(e); return { value: undefined }; });
  on('ui.close', () => ({ value: undefined }));
  if (NATIVE_VIEW) on('ui.view', ($, e) => { views.push(e); return { value: {} }; });
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('agent.list', () => {
    onAgentList?.();
    return { value: agents };
  });
  // The engine draws the prompt hint row from the tail the mod passes down.
  const tails: unknown[] = [];
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  return { tails, clock, views, focusEvents };
}

// The tail the mod hands the engine for the prompt hint row under the input.
async function hintTail($: any, tails: unknown[]): Promise<unknown> {
  tails.length = 0;
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: '' } });
  return tails.at(-1);
}

test('native Auto aliases preserve the configured digest, mapping and context', () => {
  const model = 'system.ai.claude-opus-4-6[1m]';
  const alias = 'ug-auto-111111111111-claude-opus-4-6[1m]';
  const bare = 'ug-auto-222222222222-claude-opus-4-6';
  const auto = {model: alias, mainModel: model, storeKey: 'main', description: 'Auto', models: [
    {model, label: '1M', autoModel: alias}, {model: model.slice(0, -4), label: 'bare', autoModel: bare},
  ]};
  const overrides = {[alias]: model, [bare]: model.slice(0, -4)};
  for (const selected of [alias, alias.slice(0, -4)]) {
    expect(resolveAutoModel(auto, selected)).toBe(alias);
    expect(sameAutoTarget(auto, model, selected, overrides)).toBe(true);
    expect(sameAutoTarget(auto, selected, alias, overrides)).toBe(true);
    expect(sameAutoTarget(auto, bare, selected, overrides)).toBe(false);
    expect(sameAutoTarget(auto, model.slice(0, -4), selected, overrides)).toBe(false);
    expect(sameAutoTarget(auto, model, selected, {[alias]: model.slice(0, -4)})).toBe(false);
  }
  for (const manual of [model, model.slice(0, -4), bare + '[1m]', alias.replace('111111111111', '333333333333').slice(0, -4)]) {
    expect(resolveAutoModel(auto, manual)).toBe(manual);
    expect(sameAutoTarget(auto, model, manual, overrides)).toBe(false);
  }
});

test('the band hides after a turn and returns after a toggle while the hint row tracks the state', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
  const hint = () => hintTail($, tails);
  const band = await $.ui.mount(ABOVE_PROMPT);
  expect(hasBand(await band.drawn())).toBe(true);
  expect(await hint()).toBe('smart router ON');

  // The band stays through the prompt it first appears with.
  await $.prompt.submit({ text: 'first' });
  expect(hasBand(await band.drawn())).toBe(true);
  await $.turn.complete({ answer: 'done' });
  const hidden = await band.drawn();
  expect(hasBand(hidden)).toBe(false);
  expect(JSON.stringify(hidden)).toContain('theirs');
  // The hint row keeps the state once the band hides.
  expect(await hint()).toBe('smart router ON');

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBe('Smart Router is off');
  expect(hasBand(await band.drawn())).toBe(true);
  // The hint row flips with the toggle, not after the next turn.
  expect(await hint()).toBe('smart router OFF');
  await $.prompt.submit({ text: 'second' });
  await $.turn.complete({ answer: 'done' });
  expect(hasBand(await band.drawn())).toBe(false);
  expect(await hint()).toBe('smart router OFF');
});

test('the hint row reads ON or OFF from each toggle through the second turn', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
  const hint = () => hintTail($, tails);
  expect(await hint()).toBe('smart router ON');

  for (const [action, state] of [['off', 'OFF'], ['on', 'ON'], ['off', 'OFF'], ['on', 'ON']] as const) {
    await $.command.run({ command: 'smart-router', args: action });
    const expected = `smart router ${state}`;
    expect(await hint()).toBe(expected);
    for (const text of ['first', 'second']) {
      await $.prompt.submit({ text: `${text} after ${action}` });
      expect(await hint()).toBe(expected);
      await $.turn.complete({ answer: 'done' });
      expect(await hint()).toBe(expected);
    }
  }
});

test('the hint row keeps a tail another hook already set', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'theirs' } });
  expect(tails.at(-1)).toBe('theirs · smart router ON');
});

test('the hint row counts running subagents across parent turns and routing toggles', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = ['pending', 'waiting', 'idle', 'completed', 'failed', 'killed'].map((status) => ({
    id: status, description: status, type: 'general-purpose', status,
  }));
  const { tails, clock } = engine(on, env, agents);
  const row = await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'theirs' } });
  const hint = async () => {
    await clock.advance(1000);
    await row.drawn();
    return tails.at(-1);
  };
  expect(await hint()).toBe('theirs · smart router ON');
  const renders = tails.length;
  await hint();
  expect(tails.length).toBe(renders);

  agents.push({ id: 'a', description: 'First', type: 'Explore', status: 'running' });
  expect(await hint()).toBe('theirs · smart router ON · 1 subagent running');
  agents.push({ id: 'b', description: 'Second', type: 'general-purpose', status: 'running' });
  expect(await hint()).toBe('theirs · smart router ON · 2 subagents running');

  await $.prompt.submit({ text: 'Wait for the background agents.' });
  await $.turn.complete({ answer: 'Waiting.' });
  expect(await hint()).toBe('theirs · smart router ON · 2 subagents running');
  await $.command.run({ command: 'smart-router', args: 'off' });
  expect(await hint()).toBe('theirs · smart router OFF · 2 subagents running');

  agents[agents.length - 1].status = 'completed';
  expect(await hint()).toBe('theirs · smart router OFF · 1 subagent running');
  agents[agents.length - 2].status = 'killed';
  expect(await hint()).toBe('theirs · smart router OFF');
  await row.unmount();
});

test('the band is gone by the second turn after either toggle', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const { tails } = engine(on, env);
  const band = await $.ui.mount(ABOVE_PROMPT);
  const turn = async (text: string) => {
    await $.prompt.submit({ text });
    await $.turn.complete({ answer: 'done' });
  };
  await turn('launch turn');
  expect(hasBand(await band.drawn())).toBe(false);

  for (const [action, pill] of [['off', ' OFF '], ['on', ' ON ']] as const) {
    expect((await $.command.run({ command: 'smart-router', args: action })).text).toBe(`Smart Router is ${action}`);
    const shown = await band.drawn();
    expect(hasBand(shown)).toBe(true);
    expect(JSON.stringify(shown)).toContain(pill);

    // First turn after the toggle: the band shows while it runs, then hides.
    await $.prompt.submit({ text: `first after ${action}` });
    expect(hasBand(await band.drawn())).toBe(true);
    await $.turn.complete({ answer: 'done' });
    expect(hasBand(await band.drawn())).toBe(false);

    // Second turn: the band stays gone throughout.
    await $.prompt.submit({ text: `second after ${action}` });
    expect(hasBand(await band.drawn())).toBe(false);
    await $.turn.complete({ answer: 'done' });
    expect(hasBand(await band.drawn())).toBe(false);
  }
});

const ORCHESTRATED = { ...ROUTED, orchestratorInstructions: 'Delegate under the orchestrator.' };
const CONTINUE = 'To continue a previously spawned agent, use SendMessage';
const ROUTING_GUIDANCE = "Smart Router selects the execution model for each task-role subagent. This tool's built-in model choices are not Smart Router's model catalog. Do not infer that a requested model is unavailable because it is absent from this tool's choices. For a requested name outside those choices, launch a general-purpose subagent without the model argument. Do not refuse a requested spawn on that basis. After launch, use the launch receipt to identify the model actually used. In ordinary progress updates and answers, identify only the model actually launched. Do not repeat or contrast the requested model, or discuss model availability or tool limitations, unless the user explicitly asks about that difference.";

// The orchestrator replaces the host's Agent advice but keeps its Usage notes section.
function agentDescriber($: any, on: On) {
  setup(on);
  on('tool.describe', ($, e) => ({ description: e.description }));
  return async (host: string): Promise<string> =>
    (await $.tool.describe({ tool: 'Agent', description: host })).description;
}

test('the host Usage notes survive the orchestrator description', { options: ORCHESTRATED }, async ($, on) => {
  const describe = agentDescriber($, on);
  const host = 'When in doubt, don\'t spawn.\n## Usage notes\n- Runs in the background.\n- Resume with SendMessage.\n## When to fork\nPolicy.';
  expect(await describe(host))
    .toBe(`Delegate under the orchestrator.\n\n## Usage notes\n\n- Runs in the background.\n- Resume with SendMessage.\n\n${ROUTING_GUIDANCE}`);
});

test('a host description without Usage notes keeps the continuation note', { options: ORCHESTRATED }, async ($, on) => {
  const describe = agentDescriber($, on);
  // Neither a missing section nor an empty one may drop the continuation mechanics.
  for (const host of ['Policy only.', 'Policy.\n## Usage notes\n\n## When to fork\nPolicy.']) {
    const description = await describe(host);
    expect(description.startsWith('Delegate under the orchestrator.\n\n## Usage notes\n\n- ')).toBe(true);
    expect(description).toContain(CONTINUE);
    expect(description).toContain('starts a fresh agent with no memory of prior runs');
    expect(description).not.toContain('Policy');
    expect(description).not.toContain('fork');
  }
});

test('the continuation note keeps the fork exception when the host offers forks', { options: ORCHESTRATED }, async ($, on) => {
  const describe = agentDescriber($, on);
  const host = 'Policy.\n- Use SendMessage; a new call starts fresh (except subagent_type: "fork", which inherits your context).';
  const description = await describe(host);
  expect(description).toContain('starts a fresh agent with no memory of prior runs (except subagent_type: "fork"), so the prompt');
  expect(description).not.toContain('Policy');
});


test('subagent routing guidance preserves the host description and is absent when routing is off', {options: ROUTED}, async ($, on) => {
  const {env} = setup(on);
  on('tool.describe', ($, e) => ({description: e.description}));
  const host = 'Native tool instructions and permissions.';
  const describe = () => $.tool.describe({tool: 'Agent', description: host});
  const routed = (await describe()).description;
  expect(routed).toBe(`${host}\n\n${ROUTING_GUIDANCE}`);
  env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY = '0';
  expect((await describe()).description).toBe(host);
});

const CLI_OFF = ['/ug/python', '-m', 'ucode.cli', 'claude', '--disable-smart-routing'];
const REASON = 'The routed model was too slow';
const WITH_ISAAC = { ...ROUTED, isaacExecutable: '/isaac/bin/isaac', isaacFeedbackDirectory: '/isaac/feedback' };
const FEEDBACK_PANE = {
  plugin: 'ug-smart-router', surface: 'terminal', component: 'Pane', requestId: 'smart-router-feedback',
  props: { title: 'Turn Smart Router off', isFocused: true, bodyColumns: 56, placement: 'dock', scroll: { offset: 0, bodyRows: 6 }, view: {} },
} as const;

// Isaac's feedback rule is on and the engine answers the form's panes, logs, and processes.
function feedbackSession(on: On, extra: Record<string, string> = {}, offExitCode = 0) {
  const { env } = setup(on);
  Object.assign(env, {
    ISAAC_SMART_ROUTER_FEEDBACK: '1',
    UCODE_SMART_ROUTER_PYTHON: '/ug/python',
    UCODE_SESSION_ENV_FILE: '/session/env.json',
    ...extra,
  });
  const runs: (readonly string[])[] = [];
  const logs: string[] = [];
  const panes: string[] = [];
  on('process.run', ($, e) => {
    runs.push(e.argv);
    const exitCode = e.argv.includes('--disable-smart-routing') ? offExitCode : 0;
    return { value: { exitCode, stdout: e.argv.includes('--inspect-target') ? '{"message_id":"m1"}' : '', stderr: '' } };
  });
  on('session.id', () => ({ value: 'session-1' }));
  on('fs.exists', () => ({ value: false }));
  on('ui.log', ($, e) => {
    logs.push(e.text);
    return { value: undefined };
  });
  on('ui.toast', () => ({ value: undefined }));
  on('ui.open', ($, e) => {
    panes.push(e.id);
    return { value: { isPlaced: true } };
  });
  on('ui.close', () => ({ value: undefined }));
  return { env, runs, logs, panes };
}

test('an omni launch skips the form only for an off marked --feedback-collected', { options: ROUTED }, async ($, on) => {
  const { env, runs } = feedbackSession(on);
  const off = async (args: string) => (await $.command.run({ command: 'smart-router', args })).text;

  // Without Isaac's executable the form cannot open, so a form off keeps routing on.
  expect(await off('off --feedback-collected')).toContain('Smart Router is still on');
  env.ISAAC_LAUNCH_MODE = 'omni';
  expect(await off('off')).toContain('Smart Router is still on');
  expect(runs).toEqual([]);

  expect(await off('off --feedback-collected')).toBe('Smart Router is off');
  expect(runs).toEqual([CLI_OFF]);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
});

test('--feedback-collected is accepted only after off', { options: ROUTED }, async ($, on) => {
  const { env, runs } = feedbackSession(on, { ISAAC_LAUNCH_MODE: 'omni' });
  for (const args of ['on --feedback-collected', 'off --feedback-collected now', 'off --feedback', '--feedback-collected']) {
    expect((await $.command.run({ command: 'smart-router', args })).text).toBe('Usage: /smart-router on|off|logs|tree');
  }
  expect(runs).toEqual([]);

  // Without Isaac's feedback rule the flag changes nothing.
  env.ISAAC_SMART_ROUTER_FEEDBACK = '0';
  env.ISAAC_LAUNCH_MODE = undefined;
  expect((await $.command.run({ command: 'smart-router', args: ' off  --feedback-collected ' })).text).toBe('Smart Router is off');
  expect(runs).toEqual([CLI_OFF]);
});

test('an omni off without the flag asks for the reason and logs the off', { options: WITH_ISAAC }, async ($, on) => {
  const { env, runs, logs, panes } = feedbackSession(on, { ISAAC_LAUNCH_MODE: 'omni' });

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBeUndefined();
  expect(panes).toEqual(['smart-router-feedback']);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');

  const form = await $.ui.mount(FEEDBACK_PANE);
  await form.input({ key: 'reason', text: REASON });
  expect(runs).toContainEqual([...CLI_OFF, '--smart-router-feedback', REASON]);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('0');
  // The form's off has no command reply, so the log line is its record.
  expect(logs.at(-1)).toBe('Smart Router is off');
  await form.unmount();
});

test('cancelling an omni off form keeps routing on without a reply', { options: WITH_ISAAC }, async ($, on) => {
  const { env, runs, logs } = feedbackSession(on, { ISAAC_LAUNCH_MODE: 'omni' });

  expect((await $.command.run({ command: 'smart-router', args: 'off' })).text).toBeUndefined();
  const form = await $.ui.mount(FEEDBACK_PANE);
  await form.press({ key: 'cancel' });
  expect(runs.some((argv) => argv.includes('--disable-smart-routing'))).toBe(false);
  expect(logs).toEqual([]);
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  await form.unmount();
});

test('a failed off from the form logs no off reply', { options: WITH_ISAAC }, async ($, on) => {
  const { env, logs } = feedbackSession(on, { ISAAC_LAUNCH_MODE: 'omni' }, 1);

  await $.command.run({ command: 'smart-router', args: 'off' });
  const form = await $.ui.mount(FEEDBACK_PANE);
  await form.input({ key: 'reason', text: REASON });
  expect(logs.at(-1)).toContain('Could not turn Smart Router off');
  expect(logs).not.toContain('Smart Router is off');
  expect(env.ENABLE_SMART_ROUTING_SUBAGENT_ONLY).toBe('1');
  await form.unmount();
});

const TREE_PANE = {
  plugin: 'ug-smart-router', surface: 'terminal', component: 'Pane', requestId: 'agent-tree',
  viewport: { columns: 160, rows: 50 },
  props: { title: 'Subagents', isFocused: true, bodyColumns: 76, placement: 'dock',
    scroll: { offset: 0, bodyRows: 48, contentRows: 48 }, view: {} },
} as const;

test('the subagent entry opens the tree, retains finished work, and preserves the native hint', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [];
  const { clock, tails, views } = engine(on, env, agents);
  const opens: any[] = [];
  on('ui.open', ($, e) => { opens.push(e); return { value: { isPlaced: true } }; });
  on('session.end', () => ({ sessionId: 'new-session' }));
  const hint = await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: 'native hint' } });
  expect(await hint.find({ key: 'view-subagents' })).toBeUndefined();
  expect(JSON.stringify(await hint.drawn())).not.toContain('No message');
  const emptyRenders = tails.length;
  await clock.advance(1000);
  expect(tails.length).toBe(emptyRenders);
  agents.push({ id: 'child', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running' });
  await clock.advance(1000);
  expect(await hint.find({ key: 'view-subagents' })).toBeDefined();
  expect(tails.at(-1)).toBe('native hint · smart router ON · 1 subagent running');
  expect(JSON.stringify(await hint.drawn())).toContain('View subagents');
  expect(opens.length).toBe(0);
  await hint.press({ key: 'view-subagents' });
  expect(opens.length).toBe(1);
  expect(opens[0]).toMatchObject({ id: 'agent-tree', focus: true, closeOnEscape: true });

  const pane = await $.ui.mount(TREE_PANE);
  expect(await pane.find({ type: 'Text', text: 'SUBAGENTS' })).toBeDefined();
  expect(JSON.stringify(await pane.drawn())).toContain('↩ Results return to Main');
  const childButton = await pane.find({ key: 'agent:child' });
  expect(childButton).toBeDefined();
  expect(JSON.stringify(childButton)).toContain('Otter');
  expect(JSON.stringify(childButton)).not.toContain('Otter ›');
  expect(JSON.stringify(await pane.drawn())).toContain('"label":"details"');
  if (NATIVE_VIEW) {
    // A tile press steps into the child's native transcript view, not the in-pane detail.
    await pane.press({ key: 'agent:child' });
    expect(views).toHaveLength(1);
    expect(views[0]).toMatchObject({ agentId: 'child' });
    expect(await pane.find({ type: 'Text', text: 'Launch model · not reported' })).toBeUndefined();
    await pane.press({ key: 'details:child' });
  } else {
    // Hosts without the view switch keep the read-only detail on a tile press.
    await pane.press({ key: 'agent:child' });
  }
  expect(await pane.find({ type: 'Text', text: 'Launch model · not reported' })).toBeDefined();
  agents[0].status = 'completed';
  await clock.advance(1000);
  expect(textsOf(await pane.drawn()).some(line => line.startsWith('Now · Completed'))).toBe(true);
  await clock.advance(9000);
  expect(await hint.find({ key: 'view-subagents' })).toBeDefined();
  await pane.press({ key: 'close-tree' });
  await pane.unmount();
  expect((await $.command.run({ command: 'smart-router', args: 'tree' })).text).toBeUndefined();
  expect(opens.length).toBe(2);
  const reopened = await $.ui.mount(TREE_PANE);
  expect(await reopened.find({ type: 'Text', text: 'SUBAGENTS' })).toBeDefined();
  expect(await reopened.find({ type: 'Text', text: 'Launch model · not reported' })).toBeUndefined();
  agents.length = 0;
  await $.session.end({ reason: 'clear' });
  await reopened.unmount();
  const empty = await $.ui.mount(TREE_PANE);
  expect(await empty.find({ key: 'agent:child' })).toBeUndefined();
  expect(await empty.find({ type: 'Text', text: 'Subagent activity will appear here.' })).toBeDefined();
  await empty.unmount();
  await hint.unmount();
});

for (const refused of ['denied', 'thrown']) test(`a ${refused} view switch falls back to the read-only details`, { options: ROUTED }, async ($, on) => {
  if (!NATIVE_VIEW) return;
  const { env } = setup(on);
  const agents: AgentInfo[] = [{ id: 'child-a', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running' }];
  const views: unknown[] = [];
  env.UCODE_SMART_ROUTER_PYTHON = '/ug/python';
  env.UCODE_SESSION_ENV_FILE = '/session/env.json';
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }));
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('ui.render', { component: 'Pane' }, ($, e) => $.ui.resolve(e).Text({ children: ['native pane'] }));
  on('settings.read', () => ({ value: {} }));
  on('config.list', () => ({ value: [{ key: 'theme', value: 'dark' }] }));
  on('ui.panes', () => ({ value: [] }));
  on('ui.focus', () => ({ value: undefined }));
  on('ui.close', () => ({ value: undefined }));
  on('ui.view', ($, e) => {
    views.push(e);
    if (refused === 'thrown') throw new Error('host refused the switch');
    return { value: { deny: 'not viewable' } };
  });
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('agent.list', () => ({ value: agents }));
  const pane = await $.ui.mount(TREE_PANE);
  await pane.press({ key: 'agent:child-a' });
  expect(views).toHaveLength(1);
  // The press still runs beneath the refused switch: the details fallback opens.
  expect(await pane.find({ type: 'Text', text: 'ID · child-a' })).toBeDefined();
  await pane.unmount();
});

test('stepping into a child asks for its native view and back-to-main returns to the main transcript', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [
    { id: 'child-a', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running' },
    { id: 'child-b', name: 'Heron', description: 'Read logs', type: 'Explore', status: 'running' },
  ];
  const { views } = engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  if (!NATIVE_VIEW) {
    await pane.press({ key: 'agent:child-b' });
    expect(await pane.find({ type: 'Text', text: 'Launch model · not reported' })).toBeDefined();
    expect(views).toHaveLength(0);
    await pane.unmount();
    return;
  }
  await pane.press({ key: 'agent:child-b' });
  expect(views).toHaveLength(1);
  expect(views[0]).toMatchObject({ agentId: 'child-b' });
  // The host re-renders the open pane with the view in force after the switch.
  await pane.redraw({ ...TREE_PANE.props, view: { agentId: 'child-b' } });
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('Back to main');
  expect(drawn).toContain('Viewing Heron · Back to main returns');
  expect(drawn).toContain('· in view');
  await pane.press({ key: 'back-to-main' });
  expect(views).toHaveLength(2);
  expect((views[1] as { agentId?: string }).agentId).toBeUndefined();
  // Back on the main transcript the pane drops the viewing marks.
  await pane.redraw({ ...TREE_PANE.props, view: {} });
  const restored = JSON.stringify(await pane.drawn());
  expect(restored).not.toContain('Back to main');
  expect(restored).not.toContain('Viewing Heron');
  await pane.unmount();
});

test('the focus ring paints the yellow selection and follows stable child IDs across updates', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [
    { id: 'child-a', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running' },
    { id: 'child-b', name: 'Heron', description: 'Read logs', type: 'Explore', status: 'running' },
  ];
  const { clock } = engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  // Nothing holds the ring before the person or the autoFocus element takes it:
  // the first tile offers autoFocus and no yellow is drawn yet.
  const fresh = JSON.stringify(await pane.drawn());
  expect(fresh).not.toContain('#ffd60a');
  expect(fresh).toContain('"autoFocus":true');
  await $.ui.focus({ requestId: 'agent-tree', key: 'agent:child-b' });
  // The engine redraws the site once the ring lands; the kit needs the nudge.
  await pane.redraw();
  const ringed = JSON.stringify(await pane.drawn());
  expect(ringed).toContain('#ffd60a');
  const otterCard = JSON.stringify(await pane.find({ key: 'tile:child-a' }));
  const heronCard = JSON.stringify(await pane.find({ key: 'tile:child-b' }));
  expect(otterCard).not.toContain('#ffd60a');
  expect(heronCard).toContain('#ffd60a');
  // The selection follows the same child while the roster changes underneath.
  agents.push({ id: 'child-c', name: 'Late', description: 'Joining', type: 'Explore', status: 'running' });
  await clock.advance(1000);
  await pane.redraw();
  const after = JSON.stringify(await pane.drawn());
  expect(after).toContain('#ffd60a');
  expect(JSON.stringify(await pane.find({ key: 'tile:child-b' }))).toContain('#ffd60a');
  expect(JSON.stringify(await pane.find({ key: 'tile:child-c' }))).not.toContain('#ffd60a');
  // Losing the pane's keyboard hides the selection without moving the ring.
  await pane.unmount();
  const blurred = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, isFocused: false } });
  expect(JSON.stringify(await blurred.drawn())).not.toContain('#ffd60a');
  await blurred.unmount();
});

test('the tree assigns keyboard shortcuts only while its pane is focused', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [
    { id: 'child', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running' },
    { id: 'child-b', name: 'Heron', description: 'Read logs', type: 'Explore', status: 'running' },
  ];
  engine(on, env, agents);
  const unfocused = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, isFocused: false } });
  const unfocusedDrawn = JSON.stringify(await unfocused.drawn());
  expect(unfocusedDrawn).not.toContain('"hotkey"');
  expect(JSON.stringify(await unfocused.find({ key: 'agent:child' }))).not.toContain('hotkey');
  expect(JSON.stringify(await unfocused.find({ key: 'close-tree' }))).not.toContain('hotkey');
  expect(unfocusedDrawn).toContain('ctrl+x tab focuses tiles');
  await unfocused.unmount();
  const focused = await $.ui.mount(TREE_PANE);
  const drawn = JSON.stringify(await focused.drawn());
  expect(drawn).toContain('"hotkey"');
  expect(JSON.stringify(await focused.find({ key: 'agent:child' }))).toContain('"hotkey":"1"');
  expect(JSON.stringify(await focused.find({ key: 'close-tree' }))).toContain('"hotkey":"q"');
  expect(drawn).toContain(NATIVE_VIEW ? 'Tab picks a tile · Enter steps into it' : 'Tab picks a tile · Enter opens its details');
  await focused.unmount();
});

test('paging anchors the yellow selection on the first tile of the new page', { options: ROUTED }, async ($, on) => {
  const agents: AgentInfo[] = Array.from({ length: 14 }, (_, index) => ({
    id: `child-${index}`, name: `Worker ${index}`, description: 'Same task', type: 'general-purpose',
    status: 'running',
  }));
  on('session.end', () => ({ sessionId: 'fresh' }));
  const { views } = engine(on, {}, agents);
  const pane = await $.ui.mount(TREE_PANE);
  await $.ui.focus({ requestId: 'agent-tree', key: 'agent:child-11' });
  await pane.redraw();
  expect(JSON.stringify(await pane.find({ key: 'tile:child-11' }))).toContain('#ffd60a');
  await pane.press({ key: 'next' });
  await pane.redraw();
  // Turning the page moves the ring to that page's first tile, child-12.
  expect(JSON.stringify(await pane.find({ key: 'tile:child-12' }))).toContain('#ffd60a');
  expect(JSON.stringify(await pane.find({ key: 'tile:child-11' }))).toBeUndefined();
  await pane.press({ key: 'previous' });
  await pane.redraw();
  expect(JSON.stringify(await pane.find({ key: 'tile:child-0' }))).toContain('#ffd60a');
  // A tile press while paged still steps into the right child by ID.
  await pane.press({ key: 'next' });
  await pane.press({ key: 'agent:child-13' });
  if (NATIVE_VIEW) {
    expect(views.at(-1)).toMatchObject({ agentId: 'child-13' });
  } else {
    expect(await pane.find({ type: 'Text', text: 'ID · child-13' })).toBeDefined();
  }
  // Leave the shared module state clean for the following tests.
  await $.session.end({ reason: 'clear' });
  await pane.unmount();
});

test('auto-open opens once per active batch without focus and reopens for a later batch', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  engine(on, env);
  const opens: any[] = [];
  on('turn.start', (_, e) => ({ turnId: e.turnId }));
  on('ui.open', ($, e) => { opens.push(e); return { value: { isPlaced: true } }; });

  await $.turn.start({ turnId: 'first-a', agentId: 'child-a' });
  expect(opens).toHaveLength(1);
  expect(opens[0]).toMatchObject({ id: 'agent-tree', closeOnEscape: true });
  expect(opens[0].focus).toBeUndefined();

  const pane = await $.ui.mount(TREE_PANE);
  await pane.press({ key: 'close-tree' });
  await $.turn.start({ turnId: 'first-b', agentId: 'child-b' });
  expect(opens).toHaveLength(1);

  await $.turn.complete({ turnId: 'first-a', agentId: 'child-a', answer: 'First child finished.' });
  await $.turn.complete({ turnId: 'first-b', agentId: 'child-b', answer: 'Second child finished.' });
  await $.turn.start({ turnId: 'second-a', agentId: 'child-c' });
  expect(opens).toHaveLength(2);
  expect(opens[1].focus).toBeUndefined();
  await pane.unmount();
});

test('tree auto-open opt-out still permits manual open and applies again after re-enable', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  engine(on, env);
  const opens: any[] = [];
  on('turn.start', (_, e) => ({ turnId: e.turnId }));
  on('ui.open', ($, e) => { opens.push(e); return { value: { isPlaced: true } }; });

  await $.command.run({ command: 'smart-router', args: 'tree off' });
  await $.turn.start({ turnId: 'off-turn', agentId: 'child-off' });
  expect(opens).toHaveLength(0);
  await $.command.run({ command: 'smart-router', args: 'tree' });
  expect(opens).toHaveLength(1);
  expect(opens[0]).toMatchObject({ id: 'agent-tree', focus: true });

  await $.command.run({ command: 'smart-router', args: 'tree auto' });
  const pane = await $.ui.mount(TREE_PANE);
  await pane.press({ key: 'close-tree' });
  await $.turn.start({ turnId: 'same-batch', agentId: 'child-same-batch' });
  expect(opens).toHaveLength(1);

  await $.turn.complete({ turnId: 'off-turn', agentId: 'child-off', answer: 'Opted out child finished.' });
  await $.turn.complete({ turnId: 'same-batch', agentId: 'child-same-batch', answer: 'Same batch finished.' });
  await $.turn.complete({ answer: 'The batch finished.' });
  await $.turn.start({ turnId: 'auto-turn', agentId: 'child-auto' });
  expect(opens).toHaveLength(2);
  expect(opens[1].focus).toBeUndefined();
  await pane.unmount();
});

test('the pane shows tool actions by basename only and keeps waiting, failed and privacy states', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = Array.from({ length: 4 }, (_, index) => ({
    id: `preview-${index}`, name: `Worker ${index}`, description: `Inspect parser ${index}`, type: 'Explore', status: 'running',
  }));
  const { clock } = engine(on, env, agents);
  let finishRead!: () => void;
  on('tool.call', { tool: 'Read' }, async () => {
    await new Promise<void>(resolve => { finishRead = resolve; });
    return { result: 'private file contents', text: 'private file contents' };
  });
  const pane = await $.ui.mount(TREE_PANE);
  const read = $.tool.call({ agentId: 'preview-0', tool: 'Read', tool_use_id: 'read', file_path: '/private/work/parser.ts' });
  await clock.settle();
  await clock.advance(1000);
  expect(JSON.stringify(await pane.drawn())).toContain('Reading parser.ts');
  expect(JSON.stringify(await pane.drawn())).not.toContain('/private');
  finishRead();
  await read;
  expect(JSON.stringify(await pane.drawn())).not.toContain('private file contents');
  agents[3].status = 'failed';
  await clock.advance(1000);
  expect(await pane.find({ key: 'show-failed' })).toBeDefined();
  await pane.press({ key: 'show-failed' });
  expect(await pane.find({ key: 'agent:preview-3' })).toBeDefined();
  expect(await pane.find({ key: 'agent:preview-0' })).toBeUndefined();
  await pane.press({ key: 'show-failed' });
  agents[0].status = 'waiting';
  await clock.advance(1000);
  expect(JSON.stringify(await pane.drawn())).toContain('Now · Waiting');
  await pane.unmount();
});

test('activity keeps parallel tools running and late tool results cannot replace completion', () => {
  agentTree.reset();
  const read = { agentId: 'child', tool: 'Read', tool_use_id: 'read', file_path: 'C:\\private\\parser.ts' };
  const bash = { agentId: 'child', tool: 'Bash', tool_use_id: 'bash', command: 'token=private-secret' };
  agentTree.toolStarted(read, 0);
  agentTree.toolStarted(bash, 10);
  agentTree.toolFinished(bash, {}, 20);
  const child = agentTree.nodes.get('child')!;
  expect(activityText(child)).toBe('Reading parser.ts');
  expect(child.lastTool).toBe('Running command');
  expect(child.activityAt).toBe(20);
  expect(activityMark(child, 2000)).not.toBe(activityMark(child, 2500));
  agentTree.syncAgents([{ id: 'child', type: 'Explore', description: 'Inspect parser', status: 'running' }], 1000);
  expect(child.activityAt).toBe(20);
  agentTree.syncAgents([{ id: 'child', type: 'Explore', description: 'Inspect parser', status: 'waiting' }], 1050);
  expect(activityMark(child, 2000)).toBe(activityMark(child, 2500));
  agentTree.turnStarted({ agentId: 'child', turnId: 'resumed' }, 1070);
  agentTree.syncAgents([{ id: 'child', type: 'Explore', description: 'Inspect parser', status: 'idle' }], 1060);
  expect(child.status).toBe('running');
  expect(child.endedAt).toBeUndefined();
  agentTree.turnFinished({ agentId: 'child', turnId: 'resumed', reason: 'answer' }, 1100);
  agentTree.toolFinished(read, {}, 1200);
  expect(activityText(child)).toBe('Completed');
  expect(activityMark(child, 1200)).toBe('✓');
  expect(child.toolCount).toBe(2);
  expect(JSON.stringify([...agentTree.nodes.values()])).not.toContain('private-secret');
  expect(JSON.stringify(agentTree.events)).not.toContain('private');
  agentTree.reset();
});

for (const [label, preference, animated] of [
  ['unset', undefined, true],
  ['false', false, true],
  ['true', true, false],
] as const) test(`reduced motion ${label} ${animated ? 'animates' : 'stays static'}`, { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [{
    id: 'motion-child', name: 'Motion', description: 'Watch spinner', type: 'Explore', status: 'running',
  }];
  const { clock } = engine(on, env, agents, undefined, () => preference);
  const pane = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, bodyColumns: 40 } });
  const paneMark = async () => {
    const match = JSON.stringify(await pane.drawn()).match(/children":\["([⠋⠙⠹⠸●]) Running/);
    expect(match).not.toBeNull();
    return match![1];
  };

  const firstPane = await paneMark();
  await clock.advance(1000);
  const nextPane = await paneMark();
  if (animated) {
    expect(['⠋', '⠙', '⠹', '⠸']).toContain(firstPane);
    expect(nextPane).not.toBe(firstPane);
  } else {
    expect(firstPane).toBe('●');
    expect(nextPane).toBe('●');
  }
  await pane.press({ key: 'close-tree' });
  await pane.unmount();
});

test('multilingual tasks use bounded native layout and keep their full text in details', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const task = '检查解析器行为🚀'.repeat(8);
  const agents: AgentInfo[] = [{ id: 'wide', name: '检查员🚀', description: task, type: 'Explore', status: 'running' }];
  engine(on, env, agents);
  const pane = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, bodyColumns: 24 } });
  await pane.press({ key: 'details:wide' });
  expect(JSON.stringify(await pane.drawn())).toContain(task);
  expect(JSON.stringify(await pane.drawn())).toContain('Parent · Main session');
  await pane.unmount();
});

for (const bodyColumns of [24, 54]) test(`the tree preserves every agent at ${bodyColumns} columns across pages and nested identities`, async ($, on) => {
  const agents: AgentInfo[] = Array.from({ length: 25 }, (_, index) => ({
    id: `child-${index}`, name: `Worker ${index}`, description: 'Same task', type: 'general-purpose',
    status: 'running', parentId: index === 1 ? 'child-0' : undefined,
  }));
  const say = childScript(on);
  const { clock } = engine(on, {}, agents);
  const pane = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, bodyColumns } });
  for (let index = 0; index < agents.length; index += 1) {
    await say($, agents[index].id, ['Latest statement child-' + index + '.']);
  }
  await clock.advance(500);
  expect(await pane.find({ key: 'agent:child-0' })).toBeDefined();
  expect(await pane.find({ key: 'agent:child-12' })).toBeUndefined();
  expect(JSON.stringify(await pane.drawn())).toContain('spawned by Worker 0');
  expect(JSON.stringify(await pane.drawn())).toContain('↩ Results return to Main');
  expect(JSON.stringify(await pane.drawn())).toContain('Latest statement child-0.');
  expect(JSON.stringify(await pane.drawn())).not.toContain('Latest statement child-12.');
  if (bodyColumns === 54) expect(JSON.stringify(await pane.drawn())).not.toContain('MAIN SESSION');
  await pane.press({ key: 'details:child-1' });
  expect(JSON.stringify(await pane.drawn())).toContain('Worker 1');
  expect(JSON.stringify(await pane.drawn())).toContain('Parent · Worker 0');
  expect(JSON.stringify(await pane.drawn())).toContain('ID · child-1');
  await pane.press({ key: 'pin-current' });
  expect(await pane.find({ key: 'follow-latest' })).toBeDefined();
  expect(JSON.stringify(await pane.drawn())).toContain('Latest statement child-1.');
  expect(JSON.stringify(await pane.drawn())).not.toContain('Latest statement child-0.');
  await pane.press({ key: 'follow-latest' });
  expect(JSON.stringify(await pane.drawn())).toContain('Worker 0');
  await pane.press({ key: 'next' });
  expect(await pane.find({ key: 'agent:child-12' })).toBeDefined();
  expect(JSON.stringify(await pane.drawn())).toContain('Latest statement child-12.');
  expect(JSON.stringify(await pane.drawn())).not.toContain('Latest statement child-0.');
  await pane.press({ key: 'next' });
  expect(await pane.find({ key: 'agent:child-24' })).toBeDefined();
  expect(await pane.find({ key: 'next' })).toBeUndefined();
  await pane.press({ key: 'previous' });
  expect(await pane.find({ key: 'agent:child-12' })).toBeDefined();
  await pane.unmount();
});

for (const bodyColumns of [76]) test(`full launch model identity stays in child details at ${bodyColumns} columns`, async ($, on) => {
  engine(on, {}, []);
  const model = 'long_catalog_namespace.schema.example-fast[1m]';
  on('agent.spawn', () => ({ agentId:'catalog-child', model }));
  const spawned = await $.agent.spawn({
    tool_use_id:'catalog-spawn',subagentType:'general-purpose',description:'Inspect parser',prompt:'Inspect parser',
    parentModel:'claude-sonnet-4-6',provider:{plugin:'engine',tier:'core'},background:true,fork:false,
    name:'Otter',permissionMode:'default',cwd:'/workspace',
  });
  const pane = await $.ui.mount({ ...TREE_PANE, props: { ...TREE_PANE.props, bodyColumns } });
  expect(JSON.stringify(await pane.drawn())).not.toContain(model);
  await pane.press({ key:'details:catalog-child' });
  expect(JSON.stringify(await pane.drawn())).toContain('Launch model · ' + model);
  expect(spawned.model).toBe(model);
  await pane.unmount();
});

test('the details view explains a relaunch after a denied routing attempt', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  let spawns = 0;
  on('agent.spawn', () => {
    spawns += 1;
    return spawns === 1
      ? { deny: 'router backoff' }
      : { agentId: 'child-a', model: 'routed-model' };
  });
  engine(on, env, [{ id: 'child-a', name: 'Otter', description: 'Map pane lifecycle', type: 'general-purpose', status: 'completed' }]);
  const spawn = { tool_use_id: 'spawn-denied', subagentType: 'general-purpose' as const, description: 'Map pane lifecycle',
    prompt: 'Map pane lifecycle', parentModel: 'main-model', background: false, fork: false, cwd: '/work' };
  await $.agent.spawn(spawn);
  await $.agent.spawn({ ...spawn, tool_use_id: 'spawn-ok' });
  const pane = await $.ui.mount(TREE_PANE);
  await pane.press({ key: 'details:child-a' });
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('Relaunched after a denied routing attempt');
  expect(drawn).toContain('Launch model · routed-model');
  await pane.unmount();
});

test('the tree keeps row order stable until Active first is chosen', async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'first', name: 'First', description: 'First task', type: 'Explore', status: 'running' },
    { id: 'second', name: 'Second', description: 'Second task', type: 'Explore', status: 'running' },
  ];
  const { clock } = engine(on, {}, agents);
  const pane = await $.ui.mount(TREE_PANE);
  const firstBeforeSecond = async () => {
    const drawn = JSON.stringify(await pane.drawn());
    return drawn.indexOf('agent:first') < drawn.indexOf('agent:second');
  };
  expect(await firstBeforeSecond()).toBe(true);
  agents[0].status = 'completed';
  await clock.advance(500);
  expect(await pane.find({ key: 'show-active' })).toBeDefined();
  expect(await firstBeforeSecond()).toBe(true);
  await pane.press({ key: 'show-active' });
  expect(await firstBeforeSecond()).toBe(false);
  await pane.unmount();
});

test('overview latest statements follow display order while a pinned feed stays chronological', async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'first', name: 'First', description: 'First task', type: 'Explore', status: 'running' },
    { id: 'second', name: 'Second', description: 'Second task', type: 'Explore', status: 'running' },
  ];
  const say = childScript(on);
  const { clock } = engine(on, {}, agents);
  on('turn.start', (_, e) => ({ turnId: e.turnId }));
  on('tool.call', { tool: 'Read' }, () => ({ result: '' }));
  await $.command.run({ command: 'smart-router', args: 'tree off' });
  const pane = await $.ui.mount(TREE_PANE);
  await $.turn.start({ turnId: 'child-turn', agentId: 'second' });
  await say($, 'second', ['Second latest statement.']);
  await clock.advance(10);
  await $.tool.call({ agentId: 'second', tool: 'Read', tool_use_id: 'pinned-read', file_path: '/private/parser.ts' });
  await clock.advance(10);
  await $.turn.start({ turnId: 'first-turn', agentId: 'first' });
  await $.turn.complete({ turnId: 'first-turn', agentId: 'first', answer: 'First latest statement.' });
  const firstOverview = JSON.stringify(await pane.drawn());
  expect(firstOverview).toContain('First latest statement.');
  expect(firstOverview).toContain('Second latest statement.');
  expect(firstOverview.lastIndexOf('First latest statement.')).toBeLessThan(firstOverview.lastIndexOf('Second latest statement.'));

  await clock.advance(10);
  await say($, 'second', ['Second latest statement updated.']);
  await clock.advance(500);
  const updatedOverview = JSON.stringify(await pane.drawn());
  expect(updatedOverview.lastIndexOf('First latest statement.')).toBeLessThan(updatedOverview.lastIndexOf('Second latest statement updated.'));

  await pane.press({ key: 'details:second' });
  await pane.press({ key: 'pin-current' });
  const pinned = JSON.stringify(await pane.drawn());
  const toolAt = pinned.lastIndexOf('Reading parser.ts');
  const statementAt = pinned.lastIndexOf('Second latest statement updated.');
  expect(toolAt).toBeGreaterThanOrEqual(0);
  expect(statementAt).toBeGreaterThan(toolAt);
  await pane.unmount();
});

test('the overview falls back to a quiet child answer after its timeline event is evicted', { options: ROUTED }, async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'quiet', name: 'Quiet', description: 'Preserve the latest answer', type: 'Explore', status: 'running' },
    { id: 'noisy', name: 'Noisy', description: 'Continue tool work', type: 'Explore', status: 'running' },
  ];
  const { env } = setup(on);
  const { clock } = engine(on, env, agents);
  on('turn.start', (_, e) => ({ turnId: e.turnId }));
  on('tool.call', { tool: 'Read' }, () => ({ result: '' }));
  await $.command.run({ command: 'smart-router', args: 'tree off' });
  const pane = await $.ui.mount(TREE_PANE);
  await $.turn.start({ turnId: 'quiet-turn', agentId: 'quiet' });
  await $.turn.complete({ turnId: 'quiet-turn', agentId: 'quiet', answer: 'The quiet child answer remains readable.' });
  for (let index = 0; index < 102; index += 1) {
    await $.tool.call({ agentId: 'noisy', tool: 'Read', tool_use_id: `noise-${index}`, file_path: '/private/noisy.ts' });
  }
  await clock.advance(500);
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('The quiet child answer remains readable.');
  expect(drawn).toContain('Reading noisy.ts · done');
  expect(drawn).not.toContain('/private/noisy.ts');
  await pane.unmount();
});

test('a four-row 60x20 pane keeps the first agent readable and End-scroll content reachable', { options: ROUTED }, async ($, on) => {
  const task = 'Inspect parser grammar';
  const agents: AgentInfo[] = [
    { id: 'tiny-active', name: 'Otter', description: task, type: 'Explore', status: 'running' },
    { id: 'tiny-failed', name: 'Falcon', description: 'Check the failed branch', type: 'Explore', status: 'failed' },
  ];
  const { env } = setup(on);
  const { clock } = engine(on, env, agents);
  let finishRead!: () => void;
  on('tool.call', { tool: 'Read' }, async () => {
    await new Promise<void>(resolve => { finishRead = resolve; });
    return { result: '' };
  });
  await $.command.run({ command: 'smart-router', args: 'tree off' });
  const tinyPane = {
    ...TREE_PANE,
    viewport: { columns: 60, rows: 20 },
    props: { ...TREE_PANE.props, bodyColumns: 60, scroll: { offset: 0, bodyRows: 4, contentRows: 4 } },
  } as const;
  const pane = await $.ui.mount(tinyPane);
  const read = $.tool.call({ agentId: 'tiny-active', tool: 'Read', tool_use_id: 'tiny-read', file_path: '/private/parser.ts' });
  await clock.settle();
  await clock.advance(1000);
  const firstView = await pane.drawn();
  const firstViewText = JSON.stringify(firstView);
  expect(await pane.find({ key: 'agent:tiny-active' })).toBeDefined();
  expect(firstViewText).toContain('Otter');
  expect(firstViewText).toContain(task);
  expect(firstViewText).toContain('Now · Reading parser.ts');
  expect(firstViewText).not.toContain('Main session');
  expect(await pane.find({ key: 'auto-open' })).toBeUndefined();
  expect(await pane.find({ key: 'attention:tiny-failed' })).toBeUndefined();
  expect(textsOf(firstView).some(line => line.includes('1 active'))).toBe(true);
  expect(textsOf(firstView).some(line => line.includes('1 flagged'))).toBe(true);

  finishRead();
  await read;
  await pane.press({ key: 'details:tiny-active' });
  expect(JSON.stringify(await pane.drawn())).toContain('ID · tiny-active');
  await pane.press({ key: 'all-events' });
  await pane.unmount();

  const endPane = {
    ...tinyPane,
    props: { ...tinyPane.props, scroll: { offset: 100, bodyRows: 4, contentRows: 104 } },
  } as const;
  const atEnd = await $.ui.mount(endPane);
  await clock.advance(500);
  const endText = JSON.stringify(await atEnd.drawn());
  expect(endText).toContain('subagents 0/2 done');
  expect(endText).toContain('Results return to Main');
  expect(endText).toContain('Otter');
  await atEnd.press({ key: 'details:tiny-active' });
  expect(JSON.stringify(await atEnd.drawn())).toContain('ID · tiny-active');
  await atEnd.unmount();
});

test('the tree keeps singular and plural needs-attention counts grammatical', async ($, on) => {
  const agents: AgentInfo[] = [
    { id: 'done', name: 'Otter', description: 'Check results', type: 'Explore', status: 'completed' },
    { id: 'failed-one', name: 'Falcon', description: 'Check recovery', type: 'Explore', status: 'failed' },
  ];
  const { clock } = engine(on, {}, agents);
  const pane = await $.ui.mount(TREE_PANE);
  let drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('1 done · 1 needs attention');
  expect(drawn).toContain('subagents 1/2 done · 1 needs attention');

  agents.push({ id: 'failed-two', name: 'Lynx', description: 'Check follow-up', type: 'Explore', status: 'failed' });
  await clock.advance(1000);
  drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('1 done · 2 need attention');
  expect(drawn).toContain('subagents 1/3 done · 2 need attention');
  await pane.unmount();
});

test('a finished overview stops redraws and wakes for a new child', async ($, on) => {
  const agents: AgentInfo[] = [{
    id: 'finished', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'completed',
  }];
  const { clock, tails } = engine(on, {}, agents);
  const hint = await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: {} });
  const pane = await $.ui.mount(TREE_PANE);
  await clock.advance(9000);
  expect(JSON.stringify(await pane.drawn())).toContain('Finished');
  const settledRenders = tails.length;
  await clock.advance(2000);
  expect(tails.length).toBe(settledRenders);
  agents.push({ id: 'new-child', name: 'Falcon', description: 'Check recovery', type: 'Explore', status: 'running' });
  await clock.advance(1000);
  expect(await pane.find({ key: 'agent:new-child' })).toBeDefined();
  expect(tails.at(-1)).toContain('1 subagent running');
  expect(tails.length).toBeGreaterThan(settledRenders);
  await pane.press({ key: 'close-tree' });
  await pane.unmount();
  await hint.unmount();
});

test('native pane close cancels roster refresh and clears the selected child', async ($, on) => {
  const agents: AgentInfo[] = [{
    id: 'child', name: 'Otter', description: 'Inspect parser', type: 'Explore', status: 'running',
  }];
  let agentListCalls = 0;
  const { clock } = engine(on, {}, agents, () => { agentListCalls += 1; });
  const pane = await $.ui.mount(TREE_PANE);
  await pane.press({ key: 'details:child' });
  expect(await pane.find({ type: 'Text', text: 'Launch model · not reported' })).toBeDefined();
  const callsBeforeRefresh = agentListCalls;
  await clock.advance(1000);
  expect(agentListCalls).toBeGreaterThan(callsBeforeRefresh);
  await pane.press({ key: 'close-tree' });
  const callsAtClose = agentListCalls;
  await clock.advance(1000);
  expect(agentListCalls).toBe(callsAtClose);
  await pane.unmount();

  const reopened = await $.ui.mount(TREE_PANE);
  expect(await reopened.find({ key: 'agent:child' })).toBeDefined();
  expect(await reopened.find({ type: 'Text', text: 'Launch model · not reported' })).toBeUndefined();
  await reopened.unmount();
});

test('tree lifecycle joins actual child IDs and preserves early completion', async ($, on) => {
  mock.clock(on);
  agentTree.reset();
  agentTree.routeStarted('call', 'Otter', 'Inspect parser', undefined, 100);
  agentTree.routeFinished('call', { model: 'selected-model', rationale: 'Small read-only task', seconds: 0.4 }, 500);
  const spawn = { tool_use_id: 'call', description: 'Inspect parser', subagentType: 'Explore',
    prompt: 'Private prompt must not be stored', parentModel: 'main-model', background: false, fork: false,
    cwd: '/work', provider: { plugin: 'engine', tier: 'core' as const } };
  agentTree.spawnStarted(spawn, 500);
  agentTree.turnFinished({ agentId: 'actual-child', turnId: 'first', answer: 'Parser reads tokens in /home/user/secret/lexer.py then `cat /etc/passwd`. All good.',
    durationMs: 10, reason: 'answer', isAborted: false }, 510);
  agentTree.spawnFinished(spawn, { agentId: 'actual-child', model: 'launched-model' }, 520);
  const child = agentTree.nodes.get('actual-child')!;
  expect(child.name).toBe('Otter');
  expect(child.status).toBe('completed');
  expect(child.parentId).toBe('main');
  expect(child.model).toBe('launched-model');
  expect(agentTree.routes.get('call')?.agentId).toBe('actual-child');
  agentTree.syncAgents([{ id: 'actual-child', description: 'Inspect parser', type: 'Explore', status: 'running' }], 530);
  expect(child.status).toBe('completed');
  agentTree.syncAgents([{ id: 'actual-child', description: 'Inspect parser', type: 'Explore', status: 'waiting' }], 540);
  expect(child.status).toBe('completed');
  agentTree.turnStarted({ agentId: 'actual-child', turnId: 'second', text: 'Follow-up' }, 600);
  expect(child.status).toBe('running');
  agentTree.stepStarted({ agentId: 'actual-child', turnId: 'second', index: 0, model: 'selected-model' }, 605);
  expect(child.model).toBe('launched-model');
  expect(agentTree.routes.get('call')?.model).toBe('selected-model');
  agentTree.stepFailed({ agentId: 'actual-child', turnId: 'second', index: 0, model: 'selected-model' }, 606);
  expect(child.activity).toBe('Step failed');
  agentTree.toolStarted({ agentId: 'actual-child', tool: 'Bash', tool_use_id: 'test', command: 'false' }, 610);
  agentTree.toolFinished({ agentId: 'actual-child', tool: 'Bash', tool_use_id: 'test' }, { isError: true }, 620);
  expect(child.errorCount).toBe(1);
  agentTree.turnFinished({ agentId: 'actual-child', turnId: 'second', answer: '', durationMs: 30,
    reason: 'error', isAborted: false }, 630);
  expect(child.status).toBe('failed');
  expect(JSON.stringify([...agentTree.nodes.values()])).not.toContain('Private prompt');
  expect(JSON.stringify(agentTree.events)).not.toContain('Private prompt');
  const said = JSON.stringify(agentTree.events);
  expect(said).toContain('Parser reads tokens in lexer.py');
  expect(said).not.toContain('/home/user');
  agentTree.assistantTextChunk('actual-child', 'third', 0, 'Found `project with spaces/routing-state.ts` and `/abs/dir/lexer.py` plus `rm -rf /x`.');
  expect(agentTree.nodes.get('actual-child')!.assistantText).toBe('Parser reads tokens in lexer.py then [code omitted]. All good.');
  agentTree.turnStarted({ agentId: 'actual-child', turnId: 'third' }, 650);
  agentTree.assistantTextChunk('actual-child', 'third', 0, 'Found `project with spaces/routing-state.ts` and `/abs/dir/lexer.py` plus `rm -rf /x`.', 660);
  const quote = agentTree.nodes.get('actual-child')!.assistantText!;
  expect(quote).toContain('routing-state.ts');
  expect(quote).toContain('lexer.py');
  expect(quote).not.toContain('project with spaces');
  expect(quote).not.toContain('/abs/dir');
  expect(quote).not.toContain('rm -rf');
  expect(agentTree.nodes.get('actual-child')!.activityAt).toBe(660);
  expect(agentTree.events.find(event => event.action === 'assistant message' && event.agentId === 'actual-child')?.at).toBe(660);
  expect(said).not.toContain('passwd');
  agentTree.reset();
  expect(agentTree.nodes.size).toBe(0);
  expect(agentTree.routes.size).toBe(0);
});

test('roster sync without parentId keeps the captured nested parent', () => {
  agentTree.reset();
  const spawn = { tool_use_id: 'nested-spawn', description: 'Nested audit', subagentType: 'Explore',
    prompt: 'Private nested prompt', parentModel: 'main-model', background: false, fork: false, cwd: '/work',
    parentAgentId: 'outer-child', provider: { plugin: 'engine', tier: 'core' as const } };
  agentTree.spawnStarted(spawn, 10);
  agentTree.spawnFinished(spawn, { agentId: 'deep-child' }, 20);
  expect(agentTree.nodes.get('deep-child')!.parentId).toBe('outer-child');
  // The roster snapshot omits parentId: the captured ancestry must survive.
  agentTree.syncAgents([
    { id: 'outer-child', name: 'Outer', description: 'Outer task', type: 'general-purpose', status: 'running' },
    { id: 'deep-child', description: 'Nested audit', type: 'Explore', status: 'running' },
  ], 30);
  expect(agentTree.nodes.get('deep-child')!.parentId).toBe('outer-child');
  // An explicit roster parentId stays authoritative.
  agentTree.syncAgents([{ id: 'deep-child', description: 'Nested audit', type: 'Explore', status: 'running', parentId: 'main' }], 40);
  expect(agentTree.nodes.get('deep-child')!.parentId).toBe('main');
  agentTree.reset();
});

test('a SubagentHandback never manufactures a child or flags one', () => {
  // Real shape from the routed live capture: the handback arrives with an
  // agentId that has no roster node, and its delivery can end in an error.
  agentTree.reset();
  agentTree.nodes.set('child-a', {
    id: 'child-a', parentId: 'main', name: 'Otter', role: 'general-purpose',
    task: 'Trace handback safety', status: 'completed', toolCount: 1, errorCount: 0,
    endedAt: 5, lastOutcome: 'ok',
  });
  const unknown = { tool: 'SubagentHandback', tool_use_id: 'handback-1', agentId: 'b3295b457c329c97d' };
  agentTree.toolStarted(unknown, 10);
  agentTree.toolFinished(unknown, { isError: true }, 20);
  expect([...agentTree.nodes.values()].map(node => node.id)).toEqual(['child-a']);
  const child = agentTree.nodes.get('child-a')!;
  expect(child.errorCount).toBe(0);
  expect(child.lastOutcome).toBe('ok');
  expect(child.toolCount).toBe(1);
  // A failed handback on a known child is a delivery problem, not its work.
  const own = { tool: 'SubagentHandback', tool_use_id: 'handback-2', agentId: 'child-a' };
  agentTree.toolStarted(own, 30);
  agentTree.toolFinished(own, { isError: true }, 40);
  expect(child.errorCount).toBe(0);
  expect(child.lastOutcome).toBe('ok');
  expect(child.status).toBe('completed');
  agentTree.reset();
});

test('a denied launch superseded by a relaunch folds into the child that ran', () => {
  // Test and plugin modules have separate state, so these assertions use state directly.
  agentTree.reset();
  const first = { tool_use_id: 'route-1', description: 'Map pane lifecycle', subagentType: 'general-purpose', background: true };
  const second = { tool_use_id: 'route-2', description: 'Map tree state', subagentType: 'general-purpose', background: true };
  agentTree.spawnStarted({ ...first }, 0);
  agentTree.spawnFinished({ ...first }, { deny: 'router backoff' }, 10);
  agentTree.spawnStarted({ ...second }, 11);
  agentTree.spawnFinished({ ...second }, { deny: 'router backoff' }, 12);
  const relaunch1 = { tool_use_id: 'route-3', description: 'Map pane lifecycle', subagentType: 'general-purpose', background: true };
  const relaunch2 = { tool_use_id: 'route-4', description: 'Map tree state', subagentType: 'general-purpose', background: true };
  agentTree.spawnStarted(relaunch1, 20);
  agentTree.spawnFinished(relaunch1, { agentId: 'child-a' }, 21);
  agentTree.spawnStarted(relaunch2, 22);
  agentTree.spawnFinished(relaunch2, { agentId: 'child-b' }, 23);
  const denied1 = agentTree.routes.get('route-1')!;
  const denied2 = agentTree.routes.get('route-2')!;
  expect(denied1.status).toBe('denied');
  expect(denied2.status).toBe('denied');
  expect(denied1.supersededBy).toBe('child-a');
  expect(denied2.supersededBy).toBe('child-b');
  // Exactly the two real children exist; the denials manufactured no nodes.
  expect([...agentTree.nodes.values()].map(node => node.id).sort()).toEqual(['child-a', 'child-b']);
  // The denial can also land on the Agent tool call itself (no spawn): the
  // relaunch of the same task still folds it into the child that ran.
  agentTree.routeStarted('route-6', 'Ibex', 'Map pane lifecycle', undefined, 40);
  agentTree.toolFinished({ agentId: 'main', tool: 'Agent', tool_use_id: 'route-6' }, { deny: 'router backoff' }, 41);
  expect(agentTree.routes.get('route-6')?.status).toBe('denied');
  agentTree.routeStarted('route-7', 'Elk', 'Map pane lifecycle', undefined, 42);
  agentTree.spawnStarted({ tool_use_id: 'route-7', description: 'Map pane lifecycle', subagentType: 'general-purpose', background: true }, 43);
  agentTree.spawnFinished({ tool_use_id: 'route-7', description: 'Map pane lifecycle', subagentType: 'general-purpose', background: true }, { agentId: 'child-c' }, 44);
  expect(agentTree.routes.get('route-6')?.supersededBy).toBe('child-c');
  // An unsuperseded denial stays denied and renders as needs attention.
  agentTree.spawnStarted({ tool_use_id: 'route-5', description: 'Solo retry', subagentType: 'general-purpose', background: true }, 30);
  agentTree.spawnFinished({ tool_use_id: 'route-5', description: 'Solo retry', subagentType: 'general-purpose', background: true }, { deny: 'no capacity' }, 45);
  expect(agentTree.routes.get('route-5')?.status).toBe('denied');
  expect(agentTree.routes.get('route-5')?.supersededBy).toBeUndefined();
  agentTree.reset();
});

test('a denied spawn and its enclosing tool count one error for the same owner', () => {
  for (const parentAgentId of [undefined, 'parent-child']) {
    for (const result of [{ isError: true }, { deny: 'Permission denied' }]) {
      agentTree.reset();
      const tool = { agentId: parentAgentId, tool: 'Agent', tool_use_id: 'spawn' };
      const spawn = { tool_use_id: 'spawn', description: 'Inspect parser', subagentType: 'Explore',
        parentAgentId, background: true };
      agentTree.toolStarted(tool, 0);
      agentTree.spawnStarted(spawn, 1);
      agentTree.spawnFinished(spawn, { deny: 'Permission denied' }, 2);
      agentTree.toolFinished(tool, result, 3);
      agentTree.toolFinished(tool, result, 4);
      const owner = parentAgentId ? agentTree.nodes.get(parentAgentId)! : agentTree.main;
      expect(owner.errorCount).toBe(1);
      expect(owner.toolCount).toBe(1);
      expect(agentTree.routes.get('spawn')?.status).toBe('denied');
      const other = { agentId: parentAgentId, tool: 'Bash', tool_use_id: 'other' };
      agentTree.toolStarted(other, 5);
      agentTree.toolFinished(other, { isError: true }, 6);
      expect(owner.errorCount).toBe(2);
    }
  }
  agentTree.reset();
});

test('stream previews preserve readable paragraphs, filter shell lines, and stop at complete sentences', () => {
  const preview = safePreviewText(
    'The parser returned a useful result.\n$ cat /private/work/secret.txt\nThe next sentence remains readable.\n\nA final paragraph is intact.',
    512,
  );
  expect(preview).toBe('The parser returned a useful result.\nThe next sentence remains readable.\n\nA final paragraph is intact.');
  expect(preview).not.toContain('/private');
  expect(preview).not.toContain('cat');

  const bounded = safePreviewText('A complete sentence stays whole. '.repeat(1000), 256);
  expect(bounded.length).toBeLessThanOrEqual(256);
  expect(bounded.endsWith('.')).toBe(true);
  expect(bounded).not.toContain('…');
});

test('duplicate and stale turn answers preserve the first final until an explicit resume', () => {
  agentTree.reset();
  agentTree.turnStarted({ agentId: 'child', turnId: 'first' }, 10);
  agentTree.turnFinished({ agentId: 'child', turnId: 'first', answer: 'First final answer.', reason: 'answer' }, 20);
  agentTree.turnFinished({ agentId: 'child', turnId: 'first', answer: 'Duplicate stale answer.', reason: 'error' }, 30);
  const child = agentTree.nodes.get('child')!;
  expect(child.assistantText).toBe('First final answer.');
  expect(child.status).toBe('completed');
  expect(child.endedAt).toBe(20);

  agentTree.turnStarted({ agentId: 'child', turnId: 'second' }, 40);
  agentTree.stepFinished(
    { agentId: 'child', turnId: 'first', index: 0, model: 'old-model' },
    { turnId: 'stale-step', index: 0, answer: 'Late step answer.', stopReason: 'end_turn', usage: { output_tokens: 4 } },
    45,
  );
  agentTree.assistantTextChunk('child', 'first', 1, 'Late streamed answer.', 46);
  agentTree.turnFinished({ agentId: 'child', turnId: 'first', answer: 'Late stale final answer.', reason: 'error' }, 47);
  expect(child.status).toBe('running');
  expect(child.assistantText).toBe('First final answer.');
  expect(child.outputTokens).toBe(4);

  agentTree.turnFinished({ agentId: 'child', turnId: 'second', answer: 'Second final answer.', reason: 'answer' }, 50);
  expect(child.assistantText).toBe('Second final answer.');
  expect(child.status).toBe('completed');
  expect(child.endedAt).toBe(50);
  agentTree.reset();
});

test('the first active turn completion captures its answer after the roster marks the child completed', () => {
  agentTree.reset();
  const roster = { id: 'child', name: 'Otter', type: 'Explore', description: 'Inspect parser' };
  agentTree.syncAgents([{ ...roster, status: 'running' }], 10);
  agentTree.turnStarted({ agentId: roster.id, turnId: 'current' }, 20);
  agentTree.syncAgents([{ ...roster, status: 'completed' }], 30);

  const child = agentTree.nodes.get(roster.id)!;
  expect(child.status).toBe('completed');
  expect(child.endedAt).toBe(30);

  agentTree.turnFinished({ agentId: roster.id, turnId: 'current', answer: 'Current final answer.', reason: 'answer' }, 40);
  expect(child.assistantText).toBe('Current final answer.');
  expect(child.status).toBe('completed');
  expect(child.endedAt).toBe(30);

  agentTree.turnFinished({ agentId: roster.id, turnId: 'current', answer: 'Late duplicate answer.', reason: 'error' }, 50);
  expect(child.assistantText).toBe('Current final answer.');
  expect(child.lastOutcome).toBe('ok');
  expect(child.errorCount).toBe(0);
  agentTree.reset();
});

test('roster wakeups restore current activity and late results preserve idle teammates', () => {
  agentTree.reset();
  const roster = { id: 'teammate', name: 'Otter', type: 'Explore', description: 'Inspect parser', teammateId: 'team-member' };
  const read = { agentId: roster.id, tool: 'Read', tool_use_id: 'read', file_path: '/work/parser.ts' };
  agentTree.toolStarted(read, 0);
  agentTree.syncAgents([{ ...roster, status: 'waiting' }], 10);
  agentTree.syncAgents([{ ...roster, status: 'running' }], 20);
  const child = agentTree.nodes.get(roster.id)!;
  expect(activityText(child)).toBe('Reading parser.ts');
  agentTree.toolFinished(read, {}, 30);
  agentTree.syncAgents([{ ...roster, status: 'waiting' }], 40);
  agentTree.syncAgents([{ ...roster, status: 'running' }], 50);
  expect(activityText(child)).toBe('Working');
  agentTree.toolStarted({ ...read, tool_use_id: 'late-read' }, 60);
  agentTree.turnFinished({ agentId: roster.id, turnId: 'turn', reason: 'answer' }, 70);
  agentTree.toolFinished({ ...read, tool_use_id: 'late-read' }, {}, 80);
  expect(child.status).toBe('idle');
  expect(activityText(child)).toBe('Idle');
  agentTree.reset();
});

test('late model and spawn results cannot revive completed or idle children', () => {
  for (const teammate of [false, true]) {
    agentTree.reset();
    const spawn = { tool_use_id: 'spawn', description: 'Inspect parser', subagentType: 'Explore',
      background: true, ...(teammate ? { isTeammate: true as const } : {}) };
    agentTree.spawnStarted(spawn, 0);
    agentTree.syncAgents([{ id: 'child', type: 'Explore', description: 'Inspect parser', status: 'running',
      ...(teammate ? { teammateId: 'team-member' } : {}) }], 1);
    agentTree.turnFinished({ agentId: 'child', turnId: 'turn', reason: 'answer' }, 10);
    agentTree.spawnFinished(spawn, { agentId: 'child', model: 'launched-model' }, 20);
    const child = agentTree.nodes.get('child')!;
    expect(child.status).toBe(teammate ? 'idle' : 'completed');
    const step = { agentId: 'child', turnId: 'turn', index: 0, model: 'launched-model' };
    agentTree.stepFinished(step, { turnId: 'turn', index: 0, stopReason: 'end_turn', usage: { output_tokens: 3 } }, 30);
    agentTree.stepFailed(step, 40);
    expect(activityText(child)).toBe(teammate ? 'Idle' : 'Completed');
    expect(child.outputTokens).toBe(3);
    expect(child.endedAt).toBe(10);
  }
  agentTree.reset();
});

test('long sessions retain every child and exact totals while bounding the activity log', () => {
  agentTree.reset();
  let now = 0;
  for (let turn = 0; turn < 10; turn++) {
    for (let index = 0; index < 100; index++) {
      const agentId = `child-${index}`;
      const turnId = `turn-${turn}`;
      agentTree.turnStarted({ agentId, turnId }, now++);
      for (let stepIndex = 0; stepIndex < 5; stepIndex++) {
        const step = { agentId, turnId, index: stepIndex, model: 'model' };
        agentTree.stepStarted(step, now++);
        const tool = { agentId, tool: 'Read', tool_use_id: `${agentId}-${turnId}-${stepIndex}`, file_path: 'parser.ts' };
        agentTree.toolStarted(tool, now++);
        agentTree.toolFinished(tool, {}, now++);
        agentTree.toolFinished(tool, {}, now++);
        agentTree.stepFinished(step, { turnId, index: stepIndex, stopReason: 'tool_use',
          usage: { input_tokens: 10, output_tokens: 2 } }, now++);
      }
      const finish = { agentId, turnId, reason: 'answer' as const, usage: { input_tokens: 37, output_tokens: 11 } };
      agentTree.turnFinished(finish, now++);
      agentTree.turnFinished(finish, now++);
    }
  }
  expect(agentTree.nodes.size).toBe(100);
  expect(agentTree.events.length).toBe(200);
  for (const child of agentTree.nodes.values()) {
    expect(child.status).toBe('completed');
    expect(child.inputTokens).toBe(370);
    expect(child.outputTokens).toBe(110);
    expect(child.toolCount).toBe(50);
    expect(child.errorCount).toBe(0);
  }
  agentTree.reset();
  expect(agentTree.events.length).toBe(0);
  expect(agentTree.nodes.size).toBe(0);
  expect(agentTree.main.toolCount).toBe(0);
});

test('tree usage replaces step estimates with turn totals and adds resumed turns once', () => {
  agentTree.reset();
  const step = { agentId: 'child', turnId: 'first', index: 0, model: 'launch-model' };
  const usage = { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 100, cache_creation_input_tokens: 20 };
  agentTree.stepStarted(step, 100);
  agentTree.stepFinished(step, { turnId: 'first', index: 0, stopReason: 'tool_use', usage }, 110);
  agentTree.stepFinished(step, { turnId: 'first', index: 0, stopReason: 'tool_use', usage }, 111);
  expect(agentTree.nodes.get('child')?.inputTokens).toBe(130);
  const finish = { agentId: 'child', turnId: 'first', reason: 'answer' as const,
    usage: { ...usage, input_tokens: 30, output_tokens: 15, model: 'provider-backend' } };
  agentTree.turnFinished(finish, 120);
  agentTree.turnFinished(finish, 121);
  expect(agentTree.nodes.get('child')?.inputTokens).toBe(150);
  expect(agentTree.nodes.get('child')?.outputTokens).toBe(15);
  expect(agentTree.nodes.get('child')?.model).toBeUndefined();
  agentTree.turnStarted({ agentId: 'child', turnId: 'second' }, 130);
  agentTree.turnFinished({ ...finish, turnId: 'second', usage }, 140);
  expect(agentTree.nodes.get('child')?.inputTokens).toBe(280);
  expect(agentTree.nodes.get('child')?.outputTokens).toBe(20);
  agentTree.reset();
});

// Walks a drawn tree so layout assertions read props instead of matching serialized JSON.
function nodesWhere(tree: any, match: (node: any) => boolean, found: any[] = []): any[] {
  if (!tree || typeof tree !== 'object') return found;
  if (match(tree)) found.push(tree);
  for (const child of tree.children ?? []) nodesWhere(child, match, found);
  return found;
}
const textsOf = (tree: any): string[] => nodesWhere(tree, node => node.type === 'Text').map(node => (node.children ?? []).join(''));

// The engine's turn.step must be registered before the test first calls $, so the script it plays is mutable.
function childScript(on: On) {
  const script = { chunks: [] as string[] };
  on('turn.step', async function* ($, e) {
    for (const text of script.chunks) yield { kind: 'text' as const, index: 0, text };
    return { turnId: e.turnId, index: e.index, answer: script.chunks.join(''), toolUses: [], stopReason: 'end_turn' as const, usage: null };
  });
  return async ($: any, agentId: string, chunks: string[]) => {
    script.chunks = chunks;
    const stream = $.turn.step({ turnId: 'child-turn', index: 0, model: 'child-model', messageCount: 2, agentId });
    let item = await stream.next();
    while (!item.done) item = await stream.next();
  };
}

test('streamed child text becomes a sanitized excerpt on the card and the detail timeline', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents: AgentInfo[] = [{ id: 'talker', name: 'Otter', description: 'Read the lexer', type: 'Explore', status: 'running' }];
  const say = childScript(on);
  engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  const complete = 'The report preserves every complete sentence without shortening the useful result.';
  await say($, 'talker', [
    'The lexer in /home/user/secret/lexer.py splits tokens early. ', '```\ncat /etc/shadow\n```\n', '`rm -rf /tmp/x` is never called. ', '\u001b[31mColour\u0007 codes are stripped. ', 'token=sk-live-123456',
    ' The JSON config holds "api_key": "SENTINEL" and the header token: hunter2. ',
    'The env sets apiKey=abcd1234effe and the access accessToken=zzz-9999 in one line. ',
    'The split credential is token=', 'split-secret-42 across chunks. ',
    'The header was Authorization: Bearer sk-abc123 and the config had "password":"qwerty". ',
    'The probe called https://gateway.internal/v1?token=EXAMPLE&x=1 with Authorization: Basic RVhBTVBMRQ==. ', complete,
  ]);
  const card = JSON.stringify(await pane.drawn());
  expect(card).toContain('The lexer in lexer.py splits tokens early.');
  expect(card).toContain('Colour codes are stripped.');
  expect(card).toContain(complete);
  expect(card).not.toContain('…');
  for (const leaked of ['/home/user', 'shadow', 'rm -rf', 'sk-live-123456', '\\u001b', '\\u0007',
    'SENTINEL', 'hunter2', 'abcd1234effe', 'zzz-9999', 'split-secret-42',
    'sk-abc123', 'qwerty', 'EXAMPLE', 'RVhBTVBMRQ==']) expect(card).not.toContain(leaked);
  await pane.press({ key: 'details:talker' });
  const detail = JSON.stringify(await pane.drawn());
  expect(textsOf(await pane.drawn()).some(line => line.startsWith('SAYING NOW'))).toBe(true);
  expect(detail).toContain(complete);
  expect(detail).not.toContain('…');
  for (const leaked of ['SENTINEL', 'hunter2', 'abcd1234effe', 'zzz-9999', 'split-secret-42',
    'sk-abc123', 'qwerty', 'EXAMPLE', 'RVhBTVBMRQ==']) expect(detail).not.toContain(leaked);
  await pane.unmount();
});

test('native step streaming forwards every chunk and result while state keeps only public text', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const publicAnswer = 'The visible answer is complete.';
  const chunks = [
    { kind: 'thinking' as const, index: 0, text: 'PRIVATE_REASONING' },
    { kind: 'text' as const, index: 0, text: publicAnswer },
    { kind: 'tool' as const, index: 1, id: 'private-call', name: 'Read' },
    { kind: 'input' as const, index: 1, json: JSON.stringify({
      command: 'cat /private/secret.txt', result: 'PRIVATE_TOOL_RESULT', prompt: 'PRIVATE_PROMPT',
    }) },
    { kind: 'stop' as const, stopReason: 'end_turn' as const, usage: null },
  ];
  const result = {
    turnId: 'streamed-turn', index: 0, answer: publicAnswer, toolUses: [], stopReason: 'end_turn' as const,
    usage: { model: 'stream-model', input_tokens: 14, output_tokens: 7 },
  };
  const { clock } = engine(on, env, [{ id: 'streamed-child', name: 'Heron', description: 'Summarise', type: 'Explore', status: 'running' }]);
  on('turn.step', async function* ($, e) {
    for (const chunk of chunks) yield chunk;
    return { ...result, turnId: e.turnId, index: e.index };
  });

  const pane = await $.ui.mount(TREE_PANE);
  const stream = $.turn.step({ turnId: 'streamed-turn', index: 0, model: 'stream-model', messageCount: 2, agentId: 'streamed-child' });
  const forwarded: unknown[] = [];
  let item = await stream.next();
  while (!item.done) {
    forwarded.push(item.value);
    item = await stream.next();
  }
  expect(forwarded).toEqual(chunks);
  expect(item.value).toEqual(result);
  await clock.advance(500);
  expect(JSON.stringify(await pane.drawn())).toContain(publicAnswer);
  await pane.press({ key: 'details:streamed-child' });
  const state = JSON.stringify(await pane.drawn());
  expect(state).toContain('Usage · 14 input · 7 output tokens');
  for (const privateValue of ['PRIVATE_REASONING', 'PRIVATE_TOOL_RESULT', 'PRIVATE_PROMPT', '/private/secret.txt']) {
    expect(state).not.toContain(privateValue);
  }
  await pane.unmount();
});

const roster = (count: number, make: (index: number) => Partial<AgentInfo>): AgentInfo[] =>
  Array.from({ length: count }, (_, index) => ({
    id: `kid-${index}`, name: `Kid ${index}`, description: `Task ${index}`, type: 'Explore', status: 'running', ...make(index),
  }) as AgentInfo);

test('running and completed children retain the same card hierarchy and real task text', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents = [
    { id: 'active', name: 'Active', description: 'Inspect the active parser task', type: 'Explore', status: 'running' },
    { id: 'done', name: 'Done', description: 'Inspect the completed parser task', type: 'Explore', status: 'completed' },
  ] as AgentInfo[];
  engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  const drawn = await pane.drawn();
  const cards = nodesWhere(drawn, node => node.type === 'Box' && ['active', 'done'].includes(node.props?.key) && node.props.borderStyle === 'round');
  expect(cards.map(card => card.props.key).sort()).toEqual(['active', 'done']);
  const lines = textsOf(drawn);
  expect(lines).toContain('Inspect the active parser task');
  expect(lines).toContain('Inspect the completed parser task');
  expect(lines.some(line => line.startsWith('Now · Working'))).toBe(true);
  expect(lines.some(line => line.startsWith('Done') || line.includes('Finished'))).toBe(true);
  await pane.unmount();
});

test('parallel siblings stay independent in the labelled group without fabricated parent edges', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents = roster(5, index => ({ description: 'Independent task ' + index }));
  engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  const drawn = JSON.stringify(await pane.drawn());
  expect(drawn).toContain('Parallel subagents');
  for (let index = 0; index < agents.length; index += 1) {
    expect(await pane.find({ key: 'agent:kid-' + index })).toBeDefined();
    expect(drawn).toContain('Independent task ' + index);
  }
  expect(drawn).not.toContain('spawned by Kid');
  expect(drawn).not.toContain('NOT CONNECTED · parent unknown');
  await pane.unmount();
});

test('nested children indent under their real parent and unknown parents stay unconnected', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents = [
    ...roster(2, () => ({})),
    { id: 'grand', name: 'Grand', description: 'Nested twice', type: 'Explore', status: 'running', parentId: 'kid-2' },
    { id: 'mid', name: 'Mid', description: 'Nested once', type: 'Explore', status: 'running', parentId: 'kid-0' },
    { id: 'lost', name: 'Lost', description: 'Orphan', type: 'Explore', status: 'running', parentId: 'ghost' },
  ] as AgentInfo[];
  agents.push({ id: 'kid-2', name: 'Kid 2', description: 'Child of mid', type: 'Explore', status: 'running', parentId: 'mid' } as AgentInfo);
  engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  const lines = textsOf(await pane.drawn());
  expect(lines.some(line => line === '↳ spawned by Mid') || lines.some(line => line.includes('spawned by Mid'))).toBe(true);
  expect(lines.some(line => line.includes('spawned by Kid 0'))).toBe(true);
  expect(lines.some(line => line === 'NOT CONNECTED · parent unknown')).toBe(true);
  expect(lines.some(line => line.includes('spawned by ghost'))).toBe(false);
  await pane.unmount();
});

test('relative paths with spaces reduce to the basename like absolute ones', () => {
  const spaced = safePreviewText('Pane lifecycle map for project with spaces/agent-tree.ts');
  expect(spaced).not.toContain('spaces/agent-tree.ts');
  expect(spaced).toContain('agent-tree.ts');
  expect(safePreviewText('audit of /tmp/demo/project/agent-tree-state.ts')).not.toContain('/tmp/demo');
  expect(safePreviewText('audit of /tmp/demo/project/agent-tree-state.ts')).toContain('agent-tree-state.ts');
  // Plain prose and URLs are not paths.
  expect(safePreviewText('compare and/or contrast the notes')).toBe('compare and/or contrast the notes');
  expect(safePreviewText('see https://example.com/guide/intro for details')).toContain('https://example.com/guide/intro');
});

test('main activity cannot evict child history and assigned paths and secrets are sanitized', () => {
  agentTree.reset();
  agentTree.toolStarted({ agentId: 'child', tool: 'Read', tool_use_id: 'child-read', file_path: '/private/parser.ts' }, 1);
  agentTree.toolFinished({ agentId: 'child', tool: 'Read', tool_use_id: 'child-read' }, {}, 2);
  for (let index = 0; index < 250; index++) {
    agentTree.turnStarted({ turnId: 'main-' + index }, index + 3);
    agentTree.turnFinished({ turnId: 'main-' + index, answer: 'Main response' }, index + 4);
  }
  expect(agentTree.events.some(event => event.agentId === 'child' && event.action === 'tool finished')).toBe(true);
  expect(agentTree.events.some(event => event.agentId === 'main')).toBe(false);
  expect(safePreviewText('Output path=/home/private/report.txt and API_TOKEN=sensitive-value.', 1000))
    .toBe('Output path=report.txt and API_TOKEN=[redacted].');
  for (const command of ['export API_TOKEN=sensitive-value', 'env API_TOKEN=sensitive-value curl example.test']) {
    expect(safePreviewText(command, 1000)).not.toContain('sensitive-value');
  }
  agentTree.reset();
});

test('a large roster collapses finished children, pins failures and keeps stable identities', { options: ROUTED }, async ($, on) => {
  const { env } = setup(on);
  const agents = roster(16, index => ({ status: index < 10 ? 'completed' : index === 12 ? 'failed' : 'running' }));
  engine(on, env, agents);
  const pane = await $.ui.mount(TREE_PANE);
  expect(await pane.find({ key: 'agent:kid-0' })).toBeUndefined();
  expect(await pane.find({ key: 'agent:kid-10' })).toBeDefined();
  const finishedControl = nodesWhere(await pane.drawn(), node => node.props?.key === 'show-finished')[0];
  expect(finishedControl?.props?.label).toBe('10 finished · show');
  expect(await pane.find({ key: 'show-active' })).toBeDefined();
  expect(await pane.find({ key: 'show-failed' })).toBeDefined();
  expect(await pane.find({ key: 'show-finished' })).toBeDefined();
  await pane.press({ key: 'show-failed' });
  expect(await pane.find({ key: 'agent:kid-12' })).toBeDefined();
  expect(await pane.find({ key: 'agent:kid-10' })).toBeUndefined();
  await pane.press({ key: 'show-failed' });
  const attention = await pane.find({ key: 'attention:kid-12' });
  expect(attention).toBeDefined();
  await pane.press({ key: 'attention:kid-12' });
  expect(textsOf(await pane.drawn()).some(line => line.startsWith('Kid 12'))).toBe(true);
  await pane.press({ key: 'all-events' });
  await pane.press({ key: 'show-finished' });
  expect(await pane.find({ key: 'agent:kid-0' })).toBeDefined();
  await pane.unmount();
});
