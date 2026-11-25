import assert from 'node:assert/strict';
import test from 'node:test';
import {register as registerPlugin} from '../src/ucode/agents/claude_mods/register.ts';
import {registerModelSettingRecovery} from '../src/ucode/agents/claude_mods/model-setting-recovery.ts';

function hooks(options = {modelSettingCommand: ['/python', '/receipt.py', '/first.sock']}) {
  const handlers = new Map();
  const receipts = [];
  const warnings = [];
  registerModelSettingRecovery((event, matcher, handler) => handlers.set(event, handler), options);
  const $ = {
    process: {run: async (command, {stdin}) => {
      assert.deepEqual(command, options.modelSettingCommand);
      receipts.push(JSON.parse(stdin));
      return {exitCode: 0, stdout: '{"acknowledged":true}'};
    }},
    ui: {log: text => warnings.push(text)},
  };
  return {handlers, receipts, warnings, $};
}

const command = {command: 'model', args: '', origin: {kind: 'composer'}};
const selection = {key: 'model', value: 'fable', previous: 'haiku', origin: {kind: 'composer'}};

test('a delayed automatic save keeps its original command receipt', async () => {
  const {handlers, receipts, $} = hooks();
  let endCommand;
  const runningCommand = handlers.get('command.run')($, command, () => new Promise(resolve => {endCommand = resolve;}));
  await new Promise(resolve => setImmediate(resolve));
  let complete;
  const delayed = handlers.get('config.set')($, selection, async () => new Promise(resolve => {complete = resolve;}));
  await new Promise(resolve => setImmediate(resolve));
  await handlers.get('command.run')($, command, async () => ({ref: 2}));
  const result = {value: 'gateway/model-alias[1m]'};
  complete(result);
  assert.equal(await delayed, result);
  endCommand({ref: 1});
  await runningCommand;
  assert.deepEqual(receipts, [
    {event: 'command', id: 1, command: 'model', origin: 'composer'},
    {event: 'prepare', id: 2, command_id: 1},
    {event: 'command', id: 3, command: 'model', origin: 'composer'},
    {event: 'saved', id: 2, model: 'gateway/model-alias[1m]'},
  ]);
});

test('default removal uses the native raw value', async () => {
  const {handlers, receipts, $} = hooks();
  const result = {value: 'default'};
  assert.equal(await handlers.get('config.set')($, selection, async () => result), result);
  assert.deepEqual(receipts, [{event: 'prepare', id: 1}, {event: 'saved', id: 1, model: null}]);
});

test('a refusal and thrown writer failure preserve native outcomes', async () => {
  const {handlers, receipts, $} = hooks();
  const denied = {deny: 'default could not be saved'};
  assert.equal(await handlers.get('config.set')($, selection, async () => denied), denied);
  const error = new Error('native writer failed');
  await assert.rejects(handlers.get('config.set')($, selection, async () => {throw error;}), error);
  assert.deepEqual(receipts, [
    {event: 'prepare', id: 1}, {event: 'failed', id: 1},
    {event: 'prepare', id: 2}, {event: 'failed', id: 2},
  ]);
});

test('a picker command alone supplies no saved-default receipt', async () => {
  const {handlers, receipts, $} = hooks();
  const result = {ref: 8};
  assert.equal(await handlers.get('command.run')($, command, async () => result), result);
  assert.deepEqual(receipts, [{event: 'command', id: 1, command: 'model', origin: 'composer'}]);
});

test('launches without first-prompt recovery register no hooks', () => {
  assert.equal(hooks({}).handlers.size, 0);
});


test('a completed command does not own a later manual save', async () => {
  const {handlers, receipts, $} = hooks();
  await handlers.get('command.run')($, command, async () => ({ref: 1}));
  const result = {value: 'fable'};
  assert.equal(await handlers.get('config.set')($, selection, async () => result), result);
  assert.deepEqual(receipts[1], {event: 'prepare', id: 2});
});

test('a non-model command reaches native handling without a receipt wait', async () => {
  const {handlers, $} = hooks();
  let release;
  let forwarded = false;
  $.process.run = () => new Promise(resolve => {release = resolve;});
  const result = {ref: 9};
  const running = handlers.get('command.run')($, {...command, command: 'settings'}, async () => {
    forwarded = true;
    return result;
  });
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(forwarded, true);
  } finally {
    release?.({exitCode: 0, stdout: '{"acknowledged":true}'});
    assert.equal(await running, result);
  }
});

for (const failure of ['nonzero', 'unacknowledged', 'exception']) {
  test(`receipt ${failure} warns once and preserves native outcomes`, async () => {
    const {handlers, warnings, $} = hooks();
    $.process.run = async () => {
      if (failure === 'exception') throw new Error('receipt helper failed');
      return {exitCode: failure === 'nonzero' ? 1 : 0, stdout: '{"acknowledged":false}'};
    };
    const commandResult = {ref: 1};
    assert.equal(await handlers.get('command.run')($, command, async () => commandResult), commandResult);
    const saved = {value: 'fable'};
    assert.equal(await handlers.get('config.set')($, selection, async () => saved), saved);
    const error = new Error('native writer failed');
    await assert.rejects(handlers.get('config.set')($, selection, async () => {throw error;}), error);
    assert.equal(warnings.length, 1);
  });
}

test('the enabled production entry point registers saved-default receipts', async () => {
  const registered = [];
  const options = {modelSettingCommand: ['/python', '/receipt.py', '/first.sock']};
  registerPlugin((event, matcher, handler) => registered.push({event, matcher, handler}), options);
  const save = registered.find(({event, matcher}) => event === 'config.set' && matcher.key === 'model');
  assert.ok(save);
  const {receipts, $} = hooks(options);
  const result = {value: 'fable'};
  assert.equal(await save.handler($, selection, async () => result), result);
  assert.deepEqual(receipts, [{event: 'prepare', id: 1}, {event: 'saved', id: 1, model: 'fable'}]);
});
