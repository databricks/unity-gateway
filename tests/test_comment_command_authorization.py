"""Exercise comment-command job guards with synthetic GitHub event contexts."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKFLOWS = Path(__file__).parent.parent / ".github/workflows"
JOBS = [
    ("fork-integration.yml", "authorize", "/integration-test"),
    ("fork-integration.yml", "reject", "/integration-test"),
    ("fork-integration.yml", "authorize", "/integration-test abc1234"),
    ("fork-integration.yml", "reject", "/integration-test abc1234"),
    ("ug-review.yml", "review", "/ug-review"),
    ("polly-review.yml", "review", "/ug-review"),
]


def _job_accepts(workflow, job, association, own_pr, comment, *, is_pr=True):
    text = (WORKFLOWS / workflow).read_text().split(f"\n  {job}:\n", 1)[1]
    guard = re.search(r"if: >-\s*\$\{\{(.*?)\}\}", text, re.S)
    assert guard is not None
    expression = guard[1].replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    expression = " ".join(expression.split())
    event = SimpleNamespace(
        issue=SimpleNamespace(pull_request=is_pr, user=SimpleNamespace(id=101)),
        comment=SimpleNamespace(
            author_association=association,
            user=SimpleNamespace(id=101 if own_pr else 202),
            body=comment,
        ),
    )
    # These guards use only boolean operators, equality, and GitHub's three helpers.
    return bool(
        eval(
            expression,
            {"__builtins__": {}},
            {
                "github": SimpleNamespace(event_name="issue_comment", event=event),
                "contains": lambda items, value: value in items,
                "fromJSON": json.loads,
                "startsWith": str.startswith,
            },
        )
    )


@pytest.mark.parametrize(("workflow", "job", "command"), JOBS)
@pytest.mark.parametrize(
    ("association", "own_pr", "allowed"),
    [
        ("MEMBER", True, True),
        ("MEMBER", False, True),
        ("OWNER", False, True),
        ("CONTRIBUTOR", True, True),
        ("CONTRIBUTOR", False, False),
        ("FIRST_TIME_CONTRIBUTOR", True, False),
        ("FIRST_TIMER", True, False),
        ("NONE", True, False),
        ("COLLABORATOR", True, False),
    ],
)
def test_comment_command_authorizes_members_or_prior_contributors_on_own_pr(
    workflow, job, command, association, own_pr, allowed
):
    assert _job_accepts(workflow, job, association, own_pr, command) == (
        not allowed if job == "reject" else allowed
    )


@pytest.mark.parametrize(("workflow", "job", "command"), JOBS)
@pytest.mark.parametrize(("comment", "is_pr"), [("Thanks!", True), (None, False)])
def test_comment_command_ignores_ordinary_comments_and_non_pr_issues(
    workflow, job, command, comment, is_pr
):
    assert not _job_accepts(
        workflow, job, "CONTRIBUTOR", True, command if comment is None else comment, is_pr=is_pr
    )
