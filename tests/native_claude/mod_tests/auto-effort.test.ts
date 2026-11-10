import {expect, test} from 'claude-code/testing';
import {effortProfile} from '../hooks/auto-effort.ts';

test('effort profiles preserve native defaults and exclude unsupported levels', () => {
  expect(effortProfile('ug-auto-claude-opus-5-5', []).level).toBe('medium');
  expect(effortProfile('ug-auto-claude-opus-4-7', []).level).toBe('xhigh');
  expect(effortProfile('ug-auto-claude-sonnet-4-6', []).levels).toEqual(['low','medium','high','max']);
  expect(effortProfile('ug-auto-claude-haiku-4-5', []).levels).toEqual([]);
  expect(effortProfile('ug-auto-gpt-6-astra[1m]', []).levels).toEqual([]);
});

test('effort uses the lowest applicable cap across settings sources', () => {
  const id = 'ug-auto-claude-opus-5-5';
  const local = {maxEffortLevel:'low', modelSettings:{'claude-opus-5-5':{maxEffortLevel:'max'}}};
  const policy = {maxEffortLevel:'medium'};
  const profile = effortProfile(id, [local,policy]);
  expect(profile.levels).toEqual(['low','medium']);
  expect(effortProfile(id, [local,policy], undefined, 'Current effort level: max').level).toBe('medium');
});

test('native status and environment overrides outrank saved effort', () => {
  const id = 'ug-auto-claude-opus-5-5';
  const saved = {modelSettings:{'claude-opus-5-5':{effortLevel:'low'}}};
  expect(effortProfile(id, [saved], undefined, 'Current effort level: high').level).toBe('high');
  const locked = effortProfile(id, [saved], 'medium', 'Current effort level: high');
  expect(locked.level).toBe('medium');
  expect(locked.locked).toBe(true);
  expect(effortProfile(id, [saved], 'auto').level).toBe('auto');
});
