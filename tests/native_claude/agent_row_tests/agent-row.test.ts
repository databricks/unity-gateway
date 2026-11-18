import { expect, test } from 'claude-code/testing';

test('AgentRow changes only the routed child label and preserves native metadata', async ($, on) => {
  const input = { prompt: 'inspect the parser', description: 'Inspect parser', subagent_type: 'Explore' };
  let animal = '';
  const seen = new Map<string, unknown>();
  on('session.id', () => ({ value: 'session-1' }));
  on('process.run', () => ({ value: {
    exitCode: 0, stderr: '', stdout: JSON.stringify({ model: 'claude-sonnet-5', rationale: '' }),
  } }));
  on('tool.call', () => ({ result: 'ok' }));
  on('agent.spawn', () => ({ model: 'claude-sonnet-5', agentId: 'routed-child' }));
  on('ui.render', ($, e) => {
    if (e.component === 'ToolUse') animal = e.props.tool;
    if (e.component === 'AgentRow') seen.set(e.props.id, e.props);
    return { type: 'Text', props: {}, children: [e.props.label ?? e.props.tool] };
  });
  await $.tool.call({ tool: 'Agent', tool_use_id: 'routed-call', ...input });
  const tool = await $.ui.mount({
    plugin: 'ug-smart-router', component: 'ToolUse', surface: 'terminal', requestId: 'routed-call',
    props: { tool_use_id: 'routed-call', tool: 'Agent', input, isRunning: true },
  });
  await tool.unmount();
  expect(animal).toBeTruthy();
  expect(animal).not.toBe('Agent');
  await $.agent.spawn({
    tool_use_id: 'routed-call', prompt: input.prompt, description: input.description,
    subagentType: 'Explore', parentModel: 'claude-opus-4-8',
    provider: { plugin: 'engine', tier: 'core' }, background: true, fork: false,
  });
  for (const id of ['routed-child', 'unrouted-child']) {
    const props = {
      id, type: 'local_agent', status: 'running', label: 'Reading files',
      description: 'Inspect parser', tokenCount: 1234, startTime: 123,
      model: 'claude-sonnet-5', columns: 100, rowIds: [], hasRow: true,
      body: 'Native task body', isHidden: false,
    };
    const mounted = await $.ui.mount({
      plugin: 'ug-smart-router', component: 'AgentRow', surface: 'terminal',
      requestId: id, viewport: { columns: 100, rows: 30 }, props,
    });
    const label = id === 'routed-child' ? animal : props.label;
    expect(seen.get(id)).toEqual({ ...props, label });
    expect(await mounted.find({ type: 'Text', text: label })).toBeDefined();
    await mounted.unmount();
  }
});
