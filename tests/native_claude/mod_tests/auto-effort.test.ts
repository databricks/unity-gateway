import {expect, test} from 'claude-code/testing';
import {effortProfile} from '../hooks/auto-effort.ts';

test('effort defaults come from native status, not model generation', () => {
  for (const id of ['claude-opus-5-5', 'claude-future-7', 'custom']) {
    expect(effortProfile(id, []).level).toBe('auto');
    expect(effortProfile(id, [], undefined, 'Effort level: auto (currently medium)').level).toBe('medium');
    expect(effortProfile(id, [], undefined, 'Current effort level: high').level).toBe('high');
  }
});
test('settings caps and native unsupported status constrain choices', () => {
  const layers = [{maxEffortLevel:'high'}, {maxEffortLevel:'medium'}];
  expect(effortProfile('custom', layers).levels).toEqual(['low','medium']);
  expect(effortProfile('custom', [], undefined, 'Effort not supported').levels).toEqual([]);
  const locked = effortProfile('custom', [], 'low', 'Current effort level: high');
  expect(locked.level).toBe('low');
  expect(locked.locked).toBe(true);
});

for (const status of ['Current effort level: max', 'Effort level: auto (currently xhigh)']) {
  test(`known native effort is clamped to the allowed cap for ${status}`, () => {
    const profile = effortProfile('custom', [{maxEffortLevel:'high'}], undefined, status);
    expect(profile.level).toBe('high');
    expect(profile.levels).toEqual(['low','medium','high']);
  });
}
test('a cap does not invent the native default for an unvisited model', () => {
  expect(effortProfile('custom', [{maxEffortLevel:'high'}]).level).toBe('auto');
});

test('a locked environment effort remains verbatim even above the settings cap', () => {
  const profile = effortProfile('custom', [{maxEffortLevel:'low'}], 'max', 'Current effort level: high');
  expect(profile.level).toBe('max');
  expect(profile.locked).toBe(true);
});
