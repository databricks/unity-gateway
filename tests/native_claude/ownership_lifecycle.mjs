// Offline review fixture: actual registered hooks, synthetic process bridge only.
// Run with Node 24: node --experimental-strip-types <this-file> [--verify]
// --verify requires ambiguous origin-less queues to reject before forwarding.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { registerOrchestrationDecision } from '../../src/ucode/agents/claude_mods/orchestration-decision.ts';

const PIN = 'adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa';

function runtime({ answerDelivery = true, validateAnswer = () => ({ error: null }) } = {}) {
  const hooks = {};
  const prepared = [];
  const validationRequests = [];
  let state = {
    schema_version: 1, session_id: 'fixture', generation: 1,
    current: {
      request_id: 'prior', input_sha256: 'a'.repeat(64),
      policy_version: 'orchestration_v1', policy_sha256: PIN,
      decision_status: 'ok', ownership: 'main', dispatch: 'work_directly',
      owner_agent_id: null, dispatch_status: 'completed', turn_id: 'prior-turn',
    },
    owners: [],
  };
  let enabled = true;
  const status = {};
  const engine = {
    env: { get: async key => enabled && ['ENABLE_SMART_ROUTER_ORCHESTRATOR', 'ENABLE_SMART_ROUTING_SUBAGENT_ONLY'].includes(key) ? '1' : '0' },
    session: { id: async () => 'fixture' },
    agent: { list: async () => [] },
    tool: { list: async () => [{ name: 'Agent' }] },
    ui: { log: () => {}, invalidate: () => {} },
    process: {
      run: async (_command, { stdin }) => {
        const request = JSON.parse(stdin);
        let result;
        if (request.operation === 'validate_answer') {
          validationRequests.push(request);
          const response = validateAnswer(request);
          if (response && typeof response.exitCode === 'number') return response;
          result = response;
        } else if (request.operation === 'state_read') {
          result = { state: structuredClone(state) };
        } else if (request.operation === 'state_write') {
          state = structuredClone(request.state);
          result = { state };
        } else if (request.operation === 'prepare') {
          prepared.push(request.request.request_id);
          result = {
            policy_sha256: PIN, instructions: 'opaque-marker', request_id: request.request.request_id,
          };
        } else {
          throw new Error(`Unexpected bridge operation: ${request.operation}`);
        }
        return { exitCode: 0, stdout: JSON.stringify(result) };
      },
    },
  };
  registerOrchestrationDecision((name, ...arguments_) => { hooks[name] = arguments_.at(-1); }, {
    ownershipCommand: ['fixture'], ownershipPolicyVersion: 'orchestration_v1',
    ownershipPolicySha256: PIN, ownershipAnswerDelivery: answerDelivery,
  }, status);
  return {
    hooks, engine, prepared, validationRequests, status, state: () => state,
    setEnabled: value => {enabled = value;},
  };
}

async function stopResult(options, event) {
  const f = runtime(options);
  let forwarded = false;
  const result = await f.hooks['classic.Stop'](f.engine, event, async value => {
    forwarded = true;
    return { continued: true, event: value };
  });
  return { ...f, forwarded, result };
}

async function verifyStopFeedbackBoundaries() {
  const invalidText = '{"answer_parts":[]}';
  const invalid = await stopResult({
    validateAnswer: request => ({ error: request.request.final_text === invalidText
      ? 'answer_parts must contain at least one child reference' : null }),
  }, { stop_hook_active: false, last_assistant_message: invalidText });
  assert.equal(invalid.forwarded, false, 'invalid selector must block before parent stop');
  assert.match(invalid.result.block, /answer_parts must contain at least one child reference/);
  assert.deepEqual(invalid.validationRequests, [{
    operation: 'validate_answer', session_id: 'fixture', request: { final_text: invalidText },
  }]);

  for (const finalText of ['ordinary parent answer', '{"use_agent_answer":"owned-child"}']) {
    const valid = await stopResult({ validateAnswer: () => ({ error: null }) }, {
      stop_hook_active: false, last_assistant_message: finalText,
    });
    assert.equal(valid.forwarded, true, `valid/non-selector answer should pass: ${finalText}`);
    assert.equal(valid.result.continued, true);
    assert.equal(valid.validationRequests.length, 1);
  }

  const feedback = runtime({ validateAnswer: () => ({ error: 'invalid selector' }) });
  const stop = active => feedback.hooks['classic.Stop'](feedback.engine, {
    stop_hook_active: active, last_assistant_message: invalidText,
  }, async () => ({ continued: true }));
  Object.assign(feedback.state().current, {
    ownership: 'subagent', dispatch: 'spawn_subagent', dispatch_status: 'started',
  });
  assert.match((await stop(false)).block, /has not completed/);
  assert.equal(feedback.validationRequests.length, 0);
  feedback.state().current.dispatch_status = 'completed';
  assert.match((await stop(true)).block, /invalid selector/,
    'coordination feedback must not consume the answer correction opportunity');
  assert.equal((await stop(true)).continued, true, 'do not repeat our answer feedback');
  assert.equal(feedback.validationRequests.length, 1);
  feedback.state().current.request_id = 'new-request';
  assert.match((await stop(true)).block, /invalid selector/, 'a new request gets its own feedback');
  feedback.setEnabled(false);
  assert.equal((await stop(true)).continued, true);
  feedback.setEnabled(true);
  assert.match((await stop(true)).block, /invalid selector/, 'reactivation clears feedback state');
  assert.equal(feedback.validationRequests.length, 3);

  const unsupported = await stopResult({ answerDelivery: false }, {
    stop_hook_active: false, last_assistant_message: invalidText,
  });
  assert.equal(unsupported.forwarded, true, 'unsupported output surface must pass through');
  assert.equal(unsupported.validationRequests.length, 0);

  for (const event of [
    { stop_hook_active: false },
    { stop_hook_active: false, last_assistant_message: 17 },
  ]) {
    const missingField = await stopResult({}, event);
    assert.equal(missingField.forwarded, true, 'missing/non-string final text must pass through');
    assert.equal(missingField.validationRequests.length, 0);
  }

  await assert.rejects(stopResult({
    validateAnswer: () => ({
      exitCode: 2, stdout: JSON.stringify({ error: 'fixture bridge failure' }), stderr: '',
    }),
  }, { stop_hook_active: false, last_assistant_message: invalidText }), /fixture bridge failure/,
  'bridge failure must stop the turn');

  await assert.rejects(stopResult({
    validateAnswer: () => ({ exitCode: 0, stdout: JSON.stringify({ unexpected: true }), stderr: '' }),
  }, { stop_hook_active: false, last_assistant_message: invalidText }), /invalid shape/,
  'malformed response must stop the turn');
}

async function verifyResumedOwnerLifecycle() {
  const f = runtime();
  Object.assign(f.state().current, {
    ownership: 'subagent', dispatch: 'spawn_subagent',
    owner_agent_id: 'owned-child', dispatch_status: 'completed',
  });
  f.state().owners.push({ agent_id: 'owned-child', scope: 'Check files', state: 'completed' });
  const step = async agentId => {
    const stream = f.hooks['turn.step'](f.engine, {
      agentId, turnId: 'resumed-turn', index: 0,
    }, async function* () { yield { kind: 'fixture' }; });
    for await (const _ of stream) { /* Consume the real registered hook. */ }
  };
  await step('foreign-child');
  assert.equal(f.state().current.dispatch_status, 'completed');
  await step('owned-child');
  assert.equal(f.state().owners[0].state, 'running', 'resumed native owner must stop being completed');
  assert.equal(f.state().current.dispatch_status, 'started');
  const stopped = await f.hooks['classic.Stop'](f.engine, { stop_hook_active: false }, async () => ({}));
  assert.match(stopped.block, /has not completed/);
  await f.hooks['turn.complete'](f.engine, {
    agentId: 'owned-child', turnId: 'resumed-turn', reason: 'answer', isAborted: false,
  }, async e => e);
  assert.equal(f.state().owners[0].state, 'completed');
  assert.equal(f.state().current.dispatch_status, 'completed');
}

async function runReceiptFailure(diagnostics) {
  const f = runtime();
  const logs = [];
  f.engine.ui.log = message => logs.push(message);
  const originalRun = f.engine.process.run;
  f.engine.process.run = async (command, options) => {
    const envelope = JSON.parse(options.stdin);
    if (envelope.operation !== 'prepare') return originalRun(command, options);
    const receipt = {
      operation: 'orchestration_decision',
      request_id: envelope.request.request_id,
      classifier_id: 'orchestration_v1/classifier',
      provider_request_id: null,
      usage: null,
      finish_reason: null,
      status: 'error',
      ...diagnostics,
    };
    return { exitCode: 1, stdout: JSON.stringify({ error: 'fixture classifier failure', usage_receipt: receipt }) };
  };
  let error;
  try {
    await f.hooks['prompt.submit'](f.engine, {
      text: 'receipt diagnostic control', turnId: 'receipt-turn',
      origin: { kind: 'sdk' }, context: [], attachments: [],
    }, async event => event);
  } catch (caught) {
    error = caught.message;
  }
  return { error, logs };
}

async function verifyReceiptDiagnostics() {
  const accepted = await runReceiptFailure({
    failure_reason: 'x'.repeat(256),
    provider_model: 'provider/model-v1',
    response_sha256: 'a'.repeat(64),
  });
  assert.equal(accepted.error, 'fixture classifier failure');
  assert.equal(accepted.logs.length, 1);

  const nullable = await runReceiptFailure({ failure_reason: 'unavailable', provider_model: null, response_sha256: null });
  assert.equal(nullable.error, 'fixture classifier failure');
  const omitted = await runReceiptFailure({});
  assert.equal(omitted.error, 'fixture classifier failure');

  for (const diagnostics of [
    { failure_reason: '' },
    { failure_reason: ' ' },
    { failure_reason: 'x'.repeat(257) },
    { provider_model: 7 },
    { response_sha256: 'g'.repeat(64) },
  ]) {
    const rejected = await runReceiptFailure(diagnostics);
    assert.equal(rejected.error, 'ownership usage_receipt is invalid or belongs to another request');
    assert.equal(rejected.logs.length, 0);
  }
}

export async function runScenario(prompts, eventOrigin) {
  const f = runtime();
  const {hooks, engine, prepared} = f;
  for (const [text, kind] of prompts) {
    await hooks['prompt.submit'](engine, {
      text, turnId: 'prior-turn', origin: { kind }, context: [], attachments: [],
    }, async event => event);
  }
  let forwarded = false;
  let error = null;
  try {
    await hooks['turn.start'](engine, {
      text: prompts.at(-1)[0], turnId: 'next-turn',
      ...(eventOrigin ? { origin: { kind: eventOrigin } } : {}),
    }, async event => { forwarded = true; return event; });
  } catch (exception) {
    error = exception.message;
  }
  return {
    prompts, eventOrigin: eventOrigin ?? null, forwarded, error,
    preparedCount: prepared.length,
    matchesPrepared: prepared.includes(f.state().current.request_id),
    turnId: f.state().current.turn_id,
    continuationTurn: f.state().current.continuation_turn_id ?? null,
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const scenarios = [
    ['one_caller_control', [['same', 'sdk']], undefined],
    ['identical_queued_callers', [['same', 'sdk'], ['same', 'sdk']], undefined],
    ['caller_notification_collision', [['same', 'sdk'], ['same', 'task-notification']], undefined],
    ['explicit_notification_cannot_consume_caller', [['same', 'sdk']], 'task-notification'],
  ];
  const results = [];
  for (const [name, prompts, origin] of scenarios) {
    const result = await runScenario(prompts, origin);
    results.push(result);
    console.log(JSON.stringify({ name, ...result }));
  }
  assert.equal(results[0].forwarded, true);
  assert.equal(results[0].matchesPrepared, true);
  assert.equal(results[0].turnId, 'next-turn');
  assert.equal(results[3].continuationTurn, null);
  assert.equal(results[3].matchesPrepared, false);
  await verifyReceiptDiagnostics();
  await verifyStopFeedbackBoundaries();
  await verifyResumedOwnerLifecycle();
  {
    for (const result of results.slice(1, 3)) {
      assert.equal(result.forwarded, false, 'Ambiguous queue must not forward an unbound turn');
      assert.equal(typeof result.error, 'string', 'Ambiguous queue must surface an explicit error');
      assert.equal(result.continuationTurn, null);
    }
  }
}


// Exercise production callbacks directly; the only replacement is the process/host boundary.
const flow = runtime();
const {hooks, engine} = flow;
Object.assign(flow.state().current, {ownership:'subagent', dispatch:'spawn_subagent', dispatch_status:'awaiting_dispatch'});
let reads = 0;
const read = {tool:'Read',tool_use_id:'read-call'};
assert.equal(typeof (await hooks['tool.call'](engine,read,async()=>{reads++;return {result:'file'};})).deny, 'string');
assert.equal(reads,0);
const launch = {tool:'Agent', tool_use_id:'first-child'};
await hooks['tool.call'](engine,launch,async()=>({deny:'native permission denied'}));
assert.equal(flow.state().current.dispatch_status,'failed');
await hooks['tool.call'](engine,read,async()=>{reads++;return {result:'file'};});
assert.equal(reads,1,'parent may recover after a real failed dispatch');

Object.assign(flow.state().current,{ownership:'main',dispatch:'work_directly',dispatch_status:'started',owner_agent_id:null});
await hooks['turn.start'](engine,{text:'notification',turnId:'notification-turn',origin:{kind:'task-notification'}},async e=>e);
assert.equal(flow.status.value,'Main agent is not spawning subagents and is working on the task directly.');
await hooks['tool.call'](engine,{tool:'Agent',tool_use_id:'ordinary-child'}, async()=>
  hooks['agent.spawn'](engine,{tool_use_id:'ordinary-child',provider:{plugin:'engine'},description:'Independent check'},async()=>({agentId:'real-child'})));
assert.equal(flow.status.value,'Main agent is spawning subagents.');
assert.equal(flow.state().owners[0].agent_id,'real-child');
await hooks['turn.complete'](engine,{agentId:'real-child',turnId:'child-turn',reason:'answer',isAborted:false},async e=>e);
assert.equal(flow.state().owners[0].state,'completed');
assert.equal(flow.status.value,'Main agent is not spawning subagents and is working on the task directly.');

const prior = JSON.stringify(flow.state());
flow.setEnabled(false);
await hooks['prompt.submit'](engine,{text:'disabled request',origin:{kind:'sdk'},context:[]},async e=>e);
assert.equal(flow.prepared.length,0);
assert.equal(JSON.stringify(flow.state()),prior);
assert.equal(flow.status.value,undefined);
flow.setEnabled(true);
await hooks['prompt.submit'](engine,{text:'new request',origin:{kind:'sdk'},context:[]},async e=>e);
assert.equal(flow.prepared.length,1);
assert.equal(flow.state().current.dispatch_status,'undecided');
assert.equal(flow.status.value,undefined);
await hooks['session.end'](engine,{},async e=>e);
assert.equal(flow.status.value,undefined);
console.log(JSON.stringify({status:'passed',checks:['queue identity and collisions','native permission failure and parent recovery','parent verification tools','later native spawning','actual-owner completion','single-line status','routing off/on','new prompt and session-end reset','Stop-hook selector feedback boundaries'],provider:'synthetic host/process boundary; production registered hooks'}));
