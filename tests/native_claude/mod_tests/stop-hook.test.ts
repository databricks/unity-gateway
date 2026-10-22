import { expect, test } from 'claude-code/testing';

const OPTIONS = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
  ownershipCommand: ['/fixture/ownership'],
  ownershipPolicyVersion: 'orchestration_v1',
  ownershipPolicySha256: 'adca6db0480b54494fa76014d72076f020cc4a7a2f66527b54e3671a9980b1fa',
  ownershipAnswerDelivery: true,
  unset: [],
};
const ROUTING_ENV = {
  ENABLE_SMART_ROUTING_V2: '0',
  ENABLE_SMART_ROUTING_SUBAGENT_ONLY: '1',
  ENABLE_SMART_ROUTER_ORCHESTRATOR: '1',
} as const;

const INVALID_SELECTION = '{"answer_parts":[]}';

test('native Stop preserves answer feedback after another Stop block without repeating it', { options: OPTIONS }, async ($, on) => {
  const requests: Record<string, unknown>[] = [];
  on('env.get', ($, event) => ({ value: ROUTING_ENV[event.name as keyof typeof ROUTING_ENV] }));
  on('session.id', () => ({ value: 'native-test-session' }));
  on('classic.Stop', () => ({}));
  on('process.run', ($, event) => {
    expect(event.argv).toEqual(OPTIONS.ownershipCommand);
    expect(event.init.timeoutMs).toBe(10000);
    const request = JSON.parse(event.init.stdin) as Record<string, unknown>;
    requests.push(request);
    const response = request.operation === 'state_read'
      ? {
          state: {
            schema_version: 1,
            session_id: 'native-test-session',
            generation: 0,
            current: {
              request_id: 'native-test-request',
              input_sha256: 'a'.repeat(64),
              policy_version: OPTIONS.ownershipPolicyVersion,
              policy_sha256: OPTIONS.ownershipPolicySha256,
              decision_status: 'ok',
              ownership: 'main',
              dispatch: 'work_directly',
              owner_agent_id: null,
              dispatch_status: 'completed',
              turn_id: 'native-test-turn',
            },
            owners: [],
          },
        }
      : request.operation === 'validate_answer'
        ? { error: 'answer_parts must contain at least one child reference' }
        : { error: `unexpected operation: ${String(request.operation)}` };
    return { value: { exitCode: 0, stdout: JSON.stringify(response), stderr: '' } };
  });

  const result = await $.classic.Stop({
    stop_hook_active: true,
    last_assistant_message: INVALID_SELECTION,
  });

  expect(requests).toEqual([
    { operation: 'state_read', session_id: 'native-test-session' },
    {
      operation: 'validate_answer',
      session_id: 'native-test-session',
      request: { final_text: INVALID_SELECTION },
    },
  ]);
  expect(result.block).toContain('answer_parts must contain at least one child reference');
  await $.classic.Stop({ stop_hook_active: true, last_assistant_message: INVALID_SELECTION });
  expect(requests.filter(request => request.operation === 'validate_answer').length).toBe(1);
});
