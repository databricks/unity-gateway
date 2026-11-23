'use strict';

// This module is loaded from the default branch, never from the reviewed PR.
async function getPull(github, repo, pullNumber) {
  return (await github.rest.pulls.get({ ...repo, pull_number: pullNumber })).data;
}

async function eligible(github, repo, pullNumber, reviewedHead) {
  const pr = await getPull(github, repo, pullNumber);
  if (pr.state !== 'open' || pr.head.repo?.full_name !== `${repo.owner}/${repo.repo}` ||
      pr.head.sha !== reviewedHead || !pr.user?.login) return false;
  const { data } = await github.rest.repos.getCollaboratorPermissionLevel({
    ...repo, username: pr.user.login,
  });
  return ['write', 'maintain', 'admin'].includes(data.permission) ||
    ['write', 'maintain', 'admin'].includes(data.role_name);
}

async function approve(github, repo, pullNumber, reviewedHead, botLogin) {
  if (!/^[a-z0-9-]+\[bot\]$/i.test(botLogin || '')) throw new Error('Missing approver app identity');
  if (!(await eligible(github, repo, pullNumber, reviewedHead))) return false;
  const args = { ...repo, pull_number: pullNumber };
  const { data: review } = await github.rest.pulls.createReview({
    ...args, commit_id: reviewedHead, event: 'APPROVE',
    body: 'UG review recommends approval for this commit.',
  });
  // A push can race the approval request. Withdraw only this app's newly created review.
  if (review.user?.type !== 'Bot' || review.user.login !== botLogin) {
    throw new Error('Unexpected approver identity');
  }
  let current;
  try {
    current = await getPull(github, repo, pullNumber);
  } catch (error) {
    await github.rest.pulls.dismissReview({
      ...args, review_id: review.id, message: 'Could not verify the reviewed PR head.',
    });
    throw error;
  }
  if (current.state !== 'open' || current.head.sha !== reviewedHead ||
      current.head.repo?.full_name !== `${repo.owner}/${repo.repo}`) {
    await github.rest.pulls.dismissReview({
      ...args, review_id: review.id, message: 'The reviewed PR head changed.',
    });
    return false;
  }
  return true;
}

async function dismissStale(github, repo, pullNumber, botLogin) {
  if (!/^[a-z0-9-]+\[bot\]$/i.test(botLogin || '')) throw new Error('Missing approver app identity');
  const pr = await getPull(github, repo, pullNumber);
  if (pr.head.repo?.full_name !== `${repo.owner}/${repo.repo}`) return 0;
  const args = { ...repo, pull_number: pullNumber };
  const reviews = await github.paginate(github.rest.pulls.listReviews, { ...args, per_page: 100 });
  let dismissed = 0;
  for (const review of reviews) {
    if (review.state === 'APPROVED' && review.user?.type === 'Bot' &&
        review.user.login === botLogin && review.commit_id !== pr.head.sha) {
      await github.rest.pulls.dismissReview({
        ...args, review_id: review.id, message: 'A new commit was pushed to this PR.',
      });
      dismissed++;
    }
  }
  return dismissed;
}

module.exports = { eligible, approve, dismissStale };
