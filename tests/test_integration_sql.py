"""Offline coverage for the integration SQL helper's timeout contracts."""

import io
import itertools
import json
from types import SimpleNamespace

import pytest

from tests.integration.utils import sql


@pytest.fixture
def statement_states(monkeypatch):
    # Control the external SQL API and clock so warehouse waits need no credentials or sleep.
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(sql, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))

    def serve(states):
        responses = itertools.chain(states, itertools.repeat(states[-1]))

        def urlopen(_request, *, timeout):
            return io.BytesIO(
                json.dumps(
                    {
                        "statement_id": "statement",
                        "status": {"state": next(responses)},
                        "result": {"data_array": [["7"]]},
                    }
                ).encode()
            )

        monkeypatch.setattr(sql.urllib.request, "urlopen", urlopen)

    return serve


def test_cold_warehouse_does_not_consume_execution_budget(statement_states):
    statement_states(["PENDING"] * 201 + ["RUNNING", "SUCCEEDED"])

    assert sql.query_count("https://workspace", "bearer", "warehouse", "SELECT COUNT(*)", []) == 7


@pytest.mark.parametrize(
    ("state", "timeout", "message"),
    [
        ("PENDING", 4, "did not finish within 4 seconds"),
        ("PENDING", sql.STATEMENT_DEADLINE_SECONDS, "stayed PENDING"),
        ("RUNNING", sql.STATEMENT_DEADLINE_SECONDS, "180 seconds of running"),
    ],
)
def test_statement_obeys_caller_and_phase_deadlines(statement_states, state, timeout, message):
    statement_states([state])

    with pytest.raises(AssertionError, match=message):
        sql.query_count(
            "https://workspace", "bearer", "warehouse", "SELECT COUNT(*)", [], timeout=timeout
        )
