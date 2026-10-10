"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from session import Session

from ucode.smart_routing import config
from ucode.smart_routing.v2 import _claude_mod_launch_options, _write_routed_claude_plugin


@pytest.fixture(scope="module")
def native_binary():
    binary = Path(os.environ.get("UCODE_TEST_CLAUDE_BINARY", ""))
    expected = os.environ.get("UCODE_TEST_CLAUDE_VERSION", "")
    assert binary.is_absolute() and binary.is_file() and expected, (
        "Set UCODE_TEST_CLAUDE_BINARY to an absolute executable and UCODE_TEST_CLAUDE_VERSION."
    )
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected} (Claude Code)", result.stdout
    return str(binary)


def test_mod_validation_and_event_forwarding(native_binary, tmp_path):
    path = tmp_path / "plugin"
    _write_routed_claude_plugin(path, [])
    shutil.copytree(Path(__file__).with_name("mod_tests"), path / "tests")
    for command in ("validate", "test"):
        result = subprocess.run(
            [native_binary, "plugin", command, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "validate":
            assert "./register.ts hooks: session.start" in result.stdout


def test_native_options_are_launch_local(native_binary, tmp_path):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_V0_CLAUDE_ONLY,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    with (
        Session(native_binary, tmp_path / "a", env) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        assert a.observe()["options"] == _claude_mod_launch_options(env)
        assert b.observe()["options"] == _claude_mod_launch_options({})
        assert not (a.plugin / "hooks/launch.json").exists()


def test_native_toggle_lifecycle_and_isolation(native_binary, tmp_path):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_V0,
        "SMART_ROUTER_NAME": "custom_recipe",
    }
    with (
        Session(native_binary, tmp_path / "a", env) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        before, peer = a.observe(), b.observe()
        for action in ["off", "off", "on", "on"]:
            result = a.send(f"/smart-router {action}")
            assert result["num_turns"] == 0 and result["usage"]["output_tokens"] == 0
            assert f"Smart Router is {action}" in result["result"]
            expected = {
                **before,
                "environment": before["environment"]
                if action == "on"
                else dict.fromkeys(before["environment"], "0"),
                "body": {
                    "caller_field": "keep",
                    "smart_router_recipe_name": "custom_recipe" if action == "on" else "DISABLED",
                },
            }
            assert a.observe() == expected
            assert b.observe() == peer
        a.send("/smart-router off")
        a.send("/clear")
        observer = a.plugin / "hooks/observe.ts"
        observer.write_text(observer.read_text().replace("'initial'", "'reloaded'"))
        a.send("/reload-plugins")
        assert a.observe() == {
            **expected,
            "revision": "reloaded",
            "environment": dict.fromkeys(before["environment"], "0"),
            "body": {"caller_field": "keep", "smart_router_recipe_name": "DISABLED"},
        }
        a.send("/smart-router on")
        assert a.observe() == {**before, "revision": "reloaded"}


def test_native_parent_and_child_request_bodies(native_binary, fixture_api, tmp_path):
    api, captures = fixture_api
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_V0,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    with Session(native_binary, tmp_path / "requests", env, api + "/requests") as session:
        before = session.observe()
        for action in ["on", "off", "on"]:
            count = len(captures)
            command = session.send(f"/smart-router {action}")
            assert command["num_turns"] == 0 and len(captures) == count
            session.send("Use an Agent child to say hello, then confirm completion.")
            expected_flags = (
                before["environment"]
                if action == "on"
                else dict.fromkeys(before["environment"], "0")
            )
            assert json.loads((session.root / "hook-env.json").read_text()) == expected_flags
            rows = captures[count:]
            recipe = env["SMART_ROUTER_NAME"] if action == "on" else "DISABLED"
            assert {row["source"] for row in rows} == {"parent", "child"}, rows
            assert {row["value"] for row in rows} == {recipe}, rows
            assert {row["caller"] for row in rows} == {"keep"}, rows
        session.send("/smart-router off")
        session.send("/compact")
        assert session.observe()["body"] == {
            "caller_field": "keep",
            "smart_router_recipe_name": "DISABLED",
        }
        session.send("/smart-router on")
        assert session.observe() == before
