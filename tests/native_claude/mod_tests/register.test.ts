import type { AgentInfo, On } from 'claude-code';
import { expect, mock, test } from 'claude-code/testing';
import { RECIPE_FIELD } from '../hooks/constants.ts';
import { mergeExtraBody } from '../hooks/extra-body.ts';
import { parseSmartRouterCommand } from '../hooks/isaac-feedback.ts';

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
  const explain = await $.command.run({ command: 'smart-router', args: 'explain on' });
  expect(explain.text).toContain('was not enabled');
  const invalidExplain = await $.command.run({ command: 'smart-router', args: 'explain invalid' });
  expect(invalidExplain.text).toBe('Usage: /smart-router explain on|off|latest');
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
function engine(on: On, env: Record<string, string | undefined>, agents: AgentInfo[] = []) {
  const clock = mock.clock(on);
  env.UCODE_SMART_ROUTER_PYTHON = '/ug/python';
  env.UCODE_SESSION_ENV_FILE = '/session/env.json';
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }));
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ['theirs'] }));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('turn.complete', () => ({ text: '' }));
  on('agent.list', () => ({ value: agents }));
  // The engine draws the prompt hint row from the tail the mod passes down.
  const tails: unknown[] = [];
  on('ui.render', { component: 'PromptHint' }, ($, e) => {
    tails.push(e.props.tail);
    return $.ui.resolve(e).Text({ children: ['hint'] });
  });
  return { tails, clock };
}

// The tail the mod hands the engine for the prompt hint row under the input.
async function hintTail($: any, tails: unknown[]): Promise<unknown> {
  tails.length = 0;
  await $.ui.mount({ ...ABOVE_PROMPT, component: 'PromptHint', props: { tail: '' } });
  return tails.at(-1);
}

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


test('explanation commands leave routing and feedback controls untouched', { options: ROUTED }, async ($, on) => {
  const { env, writes } = setup(on);
  const initial = { ...env };
  let processes = 0;
  on('process.run', () => { processes++; return { value: { exitCode: 0, stdout: '', stderr: '' } }; });
  for (const action of ['on', 'latest', 'off']) {
    const args = ` explain\t ${action} `;
    expect(parseSmartRouterCommand(args)).toEqual({ action: 'explain', explanationAction: action });
    await $.command.run({ command: 'smart-router', args });
  }
  for (const args of ['explain', 'explain invalid', 'explain on extra', 'explain off --feedback-collected']) {
    expect(parseSmartRouterCommand(args)).toBeUndefined();
    expect((await $.command.run({ command: 'smart-router', args })).text)
      .toBe('Usage: /smart-router explain on|off|latest');
  }
  expect(env).toEqual(initial);
  expect(writes).toEqual([]);
  expect(processes).toBe(0);
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
    expect((await $.command.run({ command: 'smart-router', args })).text).toBe('Usage: /smart-router on|off|logs|explain on|off|latest');
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
