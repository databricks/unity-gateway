"""Component checks for reading persisted managed-config evidence."""

import json
from types import SimpleNamespace

import pytest

from tests.integration.utils.managed import read_persisted_managed_config

WORKSPACE = "https://workspace.invalid"


@pytest.fixture
def session(tmp_path):
    return SimpleNamespace(home=tmp_path / "session-home")


def _write_persisted_config(session, envelope):
    path = session.home / ".ucode" / "managed-config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "config",
    [
        {},
        {
            "default_agent": "unrelated-agent",
            "custom_policy": {"labels": ["independent", "policy"], "enabled": True},
        },
    ],
)
def test_read_persisted_managed_config_returns_envelope_without_enforcing_policy(session, config):
    envelope = {"workspace": WORKSPACE, "config": config, "metadata": {"revision": 7}}
    path = _write_persisted_config(session, envelope)
    original = path.read_text(encoding="utf-8")

    assert read_persisted_managed_config(session, WORKSPACE) == envelope
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("directory_instead", [False, True])
def test_read_persisted_managed_config_requires_a_file(session, directory_instead):
    if directory_instead:
        (session.home / ".ucode" / "managed-config.json").mkdir(parents=True)

    with pytest.raises(AssertionError):
        read_persisted_managed_config(session, WORKSPACE)


@pytest.mark.parametrize("contents", ["", "{", '{"workspace":', "{} trailing-data"])
def test_read_persisted_managed_config_rejects_invalid_json(session, contents):
    path = _write_persisted_config(session, {})
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        read_persisted_managed_config(session, WORKSPACE)


@pytest.mark.parametrize("envelope", [None, [], ["config"], "config", False, 7])
def test_read_persisted_managed_config_requires_a_dict_envelope(session, envelope):
    _write_persisted_config(session, envelope)

    with pytest.raises(AssertionError):
        read_persisted_managed_config(session, WORKSPACE)


@pytest.mark.parametrize(
    "workspace",
    [None, "", "https://other-workspace.invalid", WORKSPACE + "/", WORKSPACE.upper(), 7, {}, []],
)
def test_read_persisted_managed_config_requires_an_exact_workspace_match(session, workspace):
    _write_persisted_config(session, {"workspace": workspace, "config": {}})

    with pytest.raises(AssertionError):
        read_persisted_managed_config(session, WORKSPACE)


@pytest.mark.parametrize("config", [None, [], ["policy"], "policy", False, 7])
def test_read_persisted_managed_config_requires_a_dict_config(session, config):
    _write_persisted_config(session, {"workspace": WORKSPACE, "config": config})

    with pytest.raises(AssertionError):
        read_persisted_managed_config(session, WORKSPACE)


@pytest.mark.parametrize("field", ["workspace", "config"])
def test_read_persisted_managed_config_rejects_missing_fields(session, field):
    envelope = {"workspace": WORKSPACE, "config": {}}
    del envelope[field]
    _write_persisted_config(session, envelope)

    with pytest.raises(AssertionError):
        read_persisted_managed_config(session, WORKSPACE)
