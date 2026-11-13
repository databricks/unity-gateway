#!/usr/bin/env bash
# Decides whether a PR's diff goes through the Security Scan. Called by
# .github/workflows/security-scan.yml, which always runs this copy from `main`,
# so a PR cannot edit its own trust decision.
#
# Adapted from omnigent-ai/omnigent (Apache-2.0), .github/scripts/security-scan/.
#
# Trusted (not scanned): non-PR events, authors GitHub reports as
# OWNER / MEMBER / COLLABORATOR, and same-repository Dependabot. Everyone else
# is scanned, including returning CONTRIBUTORs: a past merged PR does not vouch
# for this one. author_association and the PR author login are set by GitHub,
# not by PR contents.
#
# Waiver: the `skip-security-scan` label. Applying a label needs Triage
# permission, which a fork author never has, so the label is the maintainer gate.
#
# Env in:  EVENT_NAME, AUTHOR_ASSOCIATION, PR_AUTHOR, PR_AUTHOR_TYPE,
#          PR_HEAD_REPO, REPO, PR_LABELS
# Out:     `scan=true|false` and `reason` on $GITHUB_OUTPUT.

set -euo pipefail

SKIP_LABEL="skip-security-scan"

emit() {
  echo "scan=$1" >> "$GITHUB_OUTPUT"
  echo "reason=$2" >> "$GITHUB_OUTPUT"
  echo "scan=$1 ($2)"
}

# PR_LABELS comes from the trusted resolver's GitHub API response. The scan
# runner itself needs no API access.
has_skip_label() {
  [[ "${PR_LABELS:-[]}" == *"\"$SKIP_LABEL\""* ]]
}

case "${EVENT_NAME:-}" in
  pull_request) ;;
  *)
    emit false "non-PR event (${EVENT_NAME:-unknown}); trusted context"
    exit 0
    ;;
esac

# Dependabot opens same-repo branches; a fork can't impersonate the Bot type + head repo.
if [[ "${PR_AUTHOR:-}" == 'dependabot[bot]' && "${PR_AUTHOR_TYPE:-}" == 'Bot' \
      && -n "${REPO:-}" && "${PR_HEAD_REPO:-}" == "$REPO" ]]; then
  emit false "verified same-repository Dependabot"
  exit 0
fi

case "${AUTHOR_ASSOCIATION:-}" in
  OWNER | MEMBER | COLLABORATOR)
    emit false "trusted author (author_association=$AUTHOR_ASSOCIATION)"
    ;;
  *)
    if has_skip_label; then
      emit false "'$SKIP_LABEL' waiver (label requires a Triage+ collaborator to apply)"
    else
      emit true "untrusted author (author_association=${AUTHOR_ASSOCIATION:-unknown})"
    fi
    ;;
esac
