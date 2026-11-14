"""Real UG/native Claude launch with loopback endpoint rates and prepared Messages replies."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.endpoint_rates_transport import SYNTHETIC_BEARER, WORKSPACE, EndpointRatesTransport
from tests.native_claude.messages_api import messages_api
from tests.native_claude.session import Session
from tests.native_claude.test_mod import _recorded_usage
from ucode.smart_routing import orchestrator, pricing
from ucode.workspace import workspace_key

MAIN_MODEL = "system.ai.claude-opus-4-8"
DEFAULT_MODEL = "system.ai.claude-opus-4-6"
ROUTABLE_MODELS = ["system.ai.claude-sonnet-4-6", "system.ai.claude-haiku-4-5"]
MODEL_SERVICES = sorted([MAIN_MODEL, DEFAULT_MODEL, *ROUTABLE_MODELS])
WORKSPACE_ID = "111"

_LAUNCH = """
import sys, threading
from ucode.agents import claude
from ucode.smart_routing import v2
from ucode.workspace import pin

binary, workspace, workspace_id, model, settings = sys.argv[1:]
managed = claude._managed_settings_path()
assert managed is None or not managed.exists(), 'Native launch test needs an isolated managed-settings directory'
pin(workspace, workspace_id)
try:
    v2.launch_claude(
        {'workspace': workspace, 'workspace_id': workspace_id},
        ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
         '--verbose', '--setting-sources', 'user', '--settings', settings,
         '--tools', '', '--permission-mode', 'default'],
        binary=binary, user_settings_path=claude.CLAUDE_USER_SETTINGS_PATH,
        launch_model=model, compose_settings=claude._compose_v2_settings,
        launch_model_args=claude._launch_model_args, model_name=claude._maybe_add_1m_suffix,
    )
finally:
    # Wait for the real daemon before checking the persistent cache after launch exits.
    for worker in threading.enumerate():
        if worker.name == 'ug-savings-prices':
            worker.join(timeout=35)
            assert not worker.is_alive(), 'Endpoint refresh did not finish'
"""


class SavingsLaunch(Session):
    """Reuse the native stream driver, with UG constructing all launch options and plugins."""

    def __init__(self, binary, root, transport, api):
        self.root = root
        self.transport = transport
        self.env = {
            **transport.child_environment(root),
            "CLAUDE_CONFIG_DIR": str(root / ".claude"),
            "DATABRICKS_BEARER": SYNTHETIC_BEARER,
            "ANTHROPIC_BASE_URL": api,
            "ANTHROPIC_API_KEY": "local-messages-fixture",
            "SMART_ROUTER_CONFIG_VERSION": "subagent_orch_savings_v0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_ELEGANT_MEADOW": "1",
            "DISABLE_AUTOUPDATER": "1",
            "TMPDIR": str(root / "tmp"),
        }
        (root / "tmp").mkdir()
        self.user_settings = root / ".claude/settings.json"
        self.user_settings.parent.mkdir()
        self.user_settings.write_text(
            json.dumps(
                {
                    "modelPicker": {"options": [{"model": model} for model in ROUTABLE_MODELS]},
                    "env": {
                        "ANTHROPIC_MODEL": DEFAULT_MODEL,
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": DEFAULT_MODEL,
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": ROUTABLE_MODELS[0],
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": ROUTABLE_MODELS[1],
                    },
                }
            )
        )
        self.original_settings = self.user_settings.read_bytes()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-c",
                _LAUNCH,
                binary,
                WORKSPACE,
                WORKSPACE_ID,
                MAIN_MODEL,
                str(self.user_settings),
            ],
            cwd=root,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.lines = queue.Queue()
        self.errors = []
        self.readers = [
            threading.Thread(
                target=lambda: [self.lines.put(line) for line in self.proc.stdout], daemon=True
            ),
            threading.Thread(target=lambda: self.errors.extend(self.proc.stderr), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def settings(self):
        paths = list((self.root / ".ucode").glob("claude-v2-*/settings.json"))
        assert len(paths) == 1, paths
        return paths[0], json.loads(paths[0].read_text())

    def __exit__(self, exc_type, *_args):
        self.transport.release.set()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
            raise
        finally:
            for reader in self.readers:
                reader.join(timeout=5)
            self.proc.stdout.close()
            self.proc.stderr.close()
        if exc_type is None:
            assert self.proc.returncode == 0, "".join(self.errors)
            assert self.user_settings.read_bytes() == self.original_settings


def _cache_path(root, workspace_id=WORKSPACE_ID):
    return pricing.price_cache_path(root / ".ucode", workspace_key(WORKSPACE, workspace_id))


def _assert_launch_options(session):
    path, settings = session.settings()
    options = settings["pluginConfigs"]["ug-smart-router"]["options"]
    assert options["savings_pricer"] == [
        sys.executable,
        "-P",
        "-m",
        "ucode.smart_routing.savings",
        "--price-cache",
        str(_cache_path(session.root)),
    ]
    assert options["savings_usage"] == str(path.parent / "savings-usage.json")
    assert options["orchestrator_version"]
    assert options["orchestrator_version"] == orchestrator.skill_version(agent="claude")
    assert settings["env"]["ENABLE_SMART_ROUTING_SAVINGS"] == "1"
    assert settings["env"]["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
    return options


def _assert_rates_request(transport):
    assert transport.targets == ["rates.test:443"]
    assert len(transport.requests) == 1
    request = transport.requests[0]
    url = urlsplit(request["path"])
    assert url.path == "/api/ai-gateway/v2/endpoint-rates:batchGet"
    assert parse_qsl(url.query) == [
        ("databricks_hosted_model_services", model) for model in MODEL_SERVICES
    ]
    for name in ("x-databricks-org-id", "x-databricks-workspace-id"):
        assert request["headers"][name] == WORKSPACE_ID
    assert request["headers"]["authorization"] == f"Bearer {SYNTHETIC_BEARER}"


def test_savings_launch_completes_a_turn_before_rates_arrive(native_binary, tmp_path):
    root = tmp_path / "session"
    other_cache = _cache_path(root, "222")
    pricing.write_price_cache(other_cache, {MAIN_MODEL: pricing.ModelPrice(input=Decimal("99"))})
    other_bytes = other_cache.read_bytes()
    payload = {
        "model_service_rates": [
            {
                "model_service": model,
                "costs": [
                    {
                        "unit": "USD",
                        "token_costs": [
                            {"token_type": "TOKEN_TYPE_INPUT", "cost": "1"},
                            {"token_type": "TOKEN_TYPE_OUTPUT", "cost": "5"},
                        ],
                    }
                ],
            }
            for model in MODEL_SERVICES
        ]
    }
    replies = [
        {
            "content": [{"type": "text", "text": "before rates"}],
            "usage": {"input_tokens": 7, "output_tokens": 11},
        },
        {
            "content": [{"type": "text", "text": "after rates"}],
            "usage": {"input_tokens": 13, "output_tokens": 17},
        },
    ]
    with (
        EndpointRatesTransport(tmp_path / "tls", payload, hold=True) as transport,
        messages_api(replies) as (api, requests),
        SavingsLaunch(native_binary, root, transport, api) as session,
    ):
        assert transport.requested.wait(timeout=15)
        assert session.send("Reply with before rates.")["result"] == "before rates"
        assert not transport.release.is_set() and not transport.finished.is_set()
        assert not _cache_path(root).exists()
        options = _assert_launch_options(session)
        usage = Path(options["savings_usage"])
        _recorded_usage(usage, 1, output_tokens=11)
        _assert_rates_request(transport)

        transport.release.set()
        deadline = time.monotonic() + 10
        while not _cache_path(root).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cache = json.loads(_cache_path(root).read_text())
        assert cache["version"] == 1
        assert cache["models"] == {
            model.removeprefix("system.ai."): {"input": "1", "output": "5"}
            for model in MODEL_SERVICES
        }
        assert session.send("Reply with after rates.")["result"] == "after rates"
        recorded = _recorded_usage(usage, 1, output_tokens=28)
        assert recorded["entries"][0]["usage"]["input_tokens"] == 20
        assert recorded["entries"][0]["baseline"] == MAIN_MODEL
        assert recorded["entries"][0]["served"] == MAIN_MODEL
        assert [request["model"] for request in requests] == [MAIN_MODEL, MAIN_MODEL]
    assert other_cache.read_bytes() == other_bytes


@pytest.mark.parametrize("disconnect", [False, True], ids=["http-error", "closed-connection"])
@pytest.mark.parametrize("cached", [False, True], ids=["cold-cache", "prior-cache"])
def test_savings_launch_survives_failed_rate_refresh(native_binary, tmp_path, disconnect, cached):
    root = tmp_path / "session"
    cache = _cache_path(root)
    if cached:
        pricing.write_price_cache(cache, {MAIN_MODEL: pricing.ModelPrice(input=Decimal("7"))})
    before = cache.read_bytes() if cached else None
    replies = [
        {
            "content": [{"type": "text", "text": "rates unavailable"}],
            "usage": {"input_tokens": 7, "output_tokens": 11},
        }
    ]
    with (
        EndpointRatesTransport(
            tmp_path / "tls", {}, status=503, disconnect=disconnect
        ) as transport,
        messages_api(replies) as (api, requests),
        SavingsLaunch(native_binary, root, transport, api) as session,
    ):
        assert session.send("Reply with rates unavailable.")["result"] == "rates unavailable"
        assert transport.finished.wait(timeout=10)
        _assert_rates_request(transport)
        options = _assert_launch_options(session)
        _recorded_usage(Path(options["savings_usage"]), 1, output_tokens=11)
        if not cached:
            result = subprocess.run(
                [*options["savings_pricer"], "--usage", options["savings_usage"]],
                input="",
                capture_output=True,
                text=True,
                timeout=10,
                env=session.env,
                cwd=root,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {"savings": None}
        assert [request["model"] for request in requests] == [MAIN_MODEL]
    assert (cache.read_bytes() if cache.exists() else None) == before
