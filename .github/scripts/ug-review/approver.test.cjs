'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { eligible, approve, dismissStale } = require('./approver.cjs');

const repo = { owner: 'databricks', repo: 'unity-gateway' };
const bot = 'ug-approver[bot]';
const pr = (sha = 'head', fullName = 'databricks/unity-gateway') => ({
  state: 'open', head: { sha, repo: { full_name: fullName } }, user: { login: 'author' },
});
function api({ pulls = [pr()], permission = 'write', reviews = [] } = {}) {
  let index = 0;
  const created = [], dismissed = [];
  const github = {
    rest: {
      pulls: {
        get: async () => {
          const value = pulls[Math.min(index++, pulls.length - 1)];
          if (value instanceof Error) throw value;
          return { data: value };
        },
        createReview: async (args) => {
          created.push(args);
          return { data: { id: 10, user: { type: 'Bot', login: bot } } };
        },
        dismissReview: async (args) => { dismissed.push(args); },
        listReviews: () => {},
      },
      repos: { getCollaboratorPermissionLevel: async () => ({ data: { permission } }) },
    },
    paginate: async () => reviews,
  };
  return { github, created, dismissed };
}

test('requires an open same-repository PR, matching head and write access', async () => {
  for (const permission of ['write', 'maintain', 'admin']) {
    assert.equal(await eligible(api({ permission }).github, repo, 1, 'head'), true);
  }
  for (const permission of ['read', 'triage', 'none']) {
    assert.equal(await eligible(api({ permission }).github, repo, 1, 'head'), false);
  }
  assert.equal(await eligible(api({ pulls: [pr('head', 'other/fork')] }).github, repo, 1, 'head'), false);
  assert.equal(await eligible(api({ pulls: [pr('new')] }).github, repo, 1, 'head'), false);
  assert.equal(await eligible(api({ pulls: [{ ...pr(), state: 'closed' }] }).github, repo, 1, 'head'), false);
});

test('approves only the reviewed SHA and withdraws when the head races', async () => {
  const stable = api();
  assert.equal(await approve(stable.github, repo, 1, 'head', bot), true);
  assert.equal(stable.created[0].commit_id, 'head');
  assert.equal(stable.created[0].event, 'APPROVE');
  assert.equal(stable.dismissed.length, 0);
  const moved = api({ pulls: [pr(), pr('new')] });
  assert.equal(await approve(moved.github, repo, 1, 'head', bot), false);
  assert.equal(moved.dismissed[0].review_id, 10);
  const alreadyMoved = api({ pulls: [pr('new')] });
  assert.equal(await approve(alreadyMoved.github, repo, 1, 'head', bot), false);
  assert.equal(alreadyMoved.created.length, 0);
  const failedCheck = api({ pulls: [pr(), new Error('head unavailable')] });
  await assert.rejects(approve(failedCheck.github, repo, 1, 'head', bot), /head unavailable/);
  assert.equal(failedCheck.dismissed[0].review_id, 10);
});

test('push dismissal only touches stale approvals from the configured app bot', async () => {
  const review = (id, login, type, state, commit_id) => ({ id, user: { login, type }, state, commit_id });
  const client = api({ pulls: [pr('new')], reviews: [
    review(1, bot, 'Bot', 'APPROVED', 'old'),
    review(2, bot, 'Bot', 'APPROVED', 'new'),
    review(3, bot, 'User', 'APPROVED', 'old'),
    review(4, 'human', 'User', 'APPROVED', 'old'),
    review(5, 'other[bot]', 'Bot', 'APPROVED', 'old'),
    review(6, bot, 'Bot', 'COMMENTED', 'old'),
  ] });
  assert.equal(await dismissStale(client.github, repo, 1, bot), 1);
  assert.deepEqual(client.dismissed.map((entry) => entry.review_id), [1]);
  const fork = api({ pulls: [pr('new', 'other/fork')], reviews: [review(7, bot, 'Bot', 'APPROVED', 'old')] });
  assert.equal(await dismissStale(fork.github, repo, 1, bot), 0);
});
