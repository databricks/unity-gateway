"""Provenance CUJ: ug retires only the settings it wrote and leaves a developer's own edits alone.

ug's private agent settings (``~/.claude/ucode-settings.json``, ``~/.codex/ucode.config.toml``) can
be edited after ug writes them. On a later configure ug removes a setting only when its record
proves ug wrote the current value and nobody changed it, so a value a developer or another tool
edited survives a configure whose managed config no longer supplies it, while untouched ug-written
values are still retired.

Two journeys prove preservation: a Codex ``model`` and a Claude telemetry value are edited after a
configure, and the following configure must leave the edited value (and, for Claude, the whole
telemetry group) in place. The other two are regression guards: untouched ug-written values are
still retired when the managed config stops supplying them, which earlier releases already did by
key name.

The configs are injected via ``UCODE_MANAGED_CONFIG_STUB`` (an explicit ``null`` for "no managed
default"); the config writers and workspace stay real. A non-interactive configure never writes the
OS-managed file, so these assert only the private files under the session's home.
"""

import json
import tomllib

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)

DEVELOPER_CODEX_MODEL = "developer-chosen-model"
EDITED_TRACES_ENDPOINT = "https://foreign-collector.example.invalid/v1/traces"
TRACES_ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"


def _claude_settings_path(session):
    return session.home / ".claude" / "ucode-settings.json"


def _codex_config_path(session):
    return session.home / ".codex" / "ucode.config.toml"


def _claude_settings(session):
    return json.loads(_claude_settings_path(session).read_text())


def _codex_config(session):
    return tomllib.loads(_codex_config_path(session).read_text())


def _codex_stub(session, tmp_path, *, with_default_model):
    config = (
        build_coding_agent_config(
            "CODING_AGENT_CODEX", build_codex_agent_config(models=[CODEX_TEST_MODEL])
        )
        if with_default_model
        else None
    )
    set_managed_config_stub(session, tmp_path, config)


def _claude_stub(session, tmp_path, *, tracing):
    set_managed_config_stub(
        session,
        tmp_path,
        build_coding_agent_config(
            "CODING_AGENT_CLAUDE_CODE",
            build_claude_agent_config([CLAUDE_TEST_MODEL], otel_tracing_enabled=tracing),
        ),
    )


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_developer_edited_model_survives_configure(
    live_session, workspace, tmp_path
):
    """Scenario: configure Codex against a managed config with a default model, then the developer
    changes ``model`` in ~/.codex/ucode.config.toml to their own value and adds an unrelated key,
    then configure again against a workspace whose managed config pins no default model (an
    explicit ``null`` stub).

    Expected: the first configure writes the managed default as ``model``; after the second the
    developer's edited ``model`` and the unrelated key are unchanged, because ug no longer owns the
    value it wrote. This proves preservation; earlier releases deleted ``model`` here.
    """
    session = live_session

    _codex_stub(session, tmp_path, with_default_model=True)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    written = _codex_config_path(session).read_text()
    assert _codex_config(session)["model"] == CODEX_TEST_MODEL, written

    edited = written.replace(CODEX_TEST_MODEL, DEVELOPER_CODEX_MODEL, 1)
    assert edited != written, written
    _codex_config_path(session).write_text('personality = "pragmatic"\n' + edited)

    _codex_stub(session, tmp_path, with_default_model=False)
    session.run(
        "configure", "--workspace", workspace, "--agents", "codex", "--skip-upgrade", timeout=240
    )

    config = _codex_config(session)
    assert config["model"] == DEVELOPER_CODEX_MODEL, config
    assert config["personality"] == "pragmatic", config


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_retires_its_own_model(live_session, workspace, tmp_path):
    """Scenario: configure Codex with a managed default model, add an unrelated key to
    ~/.codex/ucode.config.toml, then configure again against a workspace whose managed config pins
    no default model (``null`` stub). Nobody edits ``model``.

    Expected: the first configure writes the managed default as ``model``; the second removes that
    ug-written ``model`` while the unrelated key stays. This is a regression guard: earlier
    releases also removed ``model`` here.
    """
    session = live_session

    _codex_stub(session, tmp_path, with_default_model=True)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    written = _codex_config_path(session).read_text()
    assert _codex_config(session)["model"] == CODEX_TEST_MODEL, written

    _codex_config_path(session).write_text('personality = "pragmatic"\n' + written)

    _codex_stub(session, tmp_path, with_default_model=False)
    session.run(
        "configure", "--workspace", workspace, "--agents", "codex", "--skip-upgrade", timeout=240
    )

    config = _codex_config(session)
    assert "model" not in config, config
    assert config["personality"] == "pragmatic", config


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_edited_trace_settings_survive_tracing_off(
    live_session, workspace, tmp_path
):
    """Scenario: configure Claude with managed tracing enabled, then another tool edits
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` in ~/.claude/ucode-settings.json, then configure again
    with managed tracing off.

    Expected: tracing adds OTEL trace env keys and an ``otelHeadersHelper``; after the second
    configure the trace env keys remain, including the edited endpoint and every other trace key
    at its written value, because the group is no longer ug's to remove once a value in it was
    edited. ug's own helper is still removed: it mints workspace tokens, so it must not keep
    authenticating an exporter ug no longer manages.
    """
    session = live_session

    _claude_stub(session, tmp_path, tracing=False)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    before = _claude_settings(session)
    assert "otelHeadersHelper" not in before, before

    _claude_stub(session, tmp_path, tracing=True)
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    traced = _claude_settings(session)
    trace_keys = sorted(set(traced["env"]) - set(before["env"]))
    assert TRACES_ENDPOINT_KEY in trace_keys, trace_keys
    assert traced["env"][TRACES_ENDPOINT_KEY] != EDITED_TRACES_ENDPOINT, traced
    assert traced.get("otelHeadersHelper"), traced

    edited = json.loads(json.dumps(traced))
    edited["env"][TRACES_ENDPOINT_KEY] = EDITED_TRACES_ENDPOINT
    _claude_settings_path(session).write_text(json.dumps(edited))

    _claude_stub(session, tmp_path, tracing=False)
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    after = _claude_settings(session)
    assert after["env"].get(TRACES_ENDPOINT_KEY) == EDITED_TRACES_ENDPOINT, after
    for key in trace_keys:
        assert after["env"].get(key) == edited["env"][key], (key, after["env"])
    assert "otelHeadersHelper" not in after, after


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_retires_its_own_trace_settings(live_session, workspace, tmp_path):
    """Scenario: configure Claude with tracing off, then on, then off again, injecting each managed
    config via the stub. Nobody edits the settings between runs.

    Expected: enabling tracing adds trace env keys and an ``otelHeadersHelper`` to the private
    settings; disabling it removes every key the enabling run added (read from the file, not
    assumed) plus the helper, and every setting present before tracing was enabled is unchanged in
    key and value. This is a regression guard: earlier releases also removed these keys.
    """
    session = live_session

    _claude_stub(session, tmp_path, tracing=False)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    before = _claude_settings(session)
    assert "otelHeadersHelper" not in before, before

    _claude_stub(session, tmp_path, tracing=True)
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    traced = _claude_settings(session)
    trace_keys = sorted(set(traced["env"]) - set(before["env"]))
    assert traced.get("otelHeadersHelper"), traced
    assert any(key.startswith("OTEL_") for key in trace_keys), trace_keys

    _claude_stub(session, tmp_path, tracing=False)
    session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    after = _claude_settings(session)
    assert "otelHeadersHelper" not in after, after
    for key in trace_keys:
        assert key not in after["env"], (key, after["env"])
    assert {key: after["env"].get(key) for key in before["env"]} == before["env"], (
        before["env"],
        after["env"],
    )
