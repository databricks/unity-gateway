import assert from 'node:assert/strict';
import test from 'node:test';
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
  await handlers.get('command.run')($, command, async () => ({ref: 1}));
  let complete;
  const delayed = handlers.get('config.set')($, selection, async () => new Promise(resolve => {complete = resolve;}));
  await new Promise(resolve => setImmediate(resolve));
  await handlers.get('command.run')($, command, async () => ({ref: 2}));
  const result = {value: 'gateway/model-alias[1m]'};
  complete(result);
  assert.equal(await delayed, result);
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
