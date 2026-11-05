"""Offline checks for the managed Codex state-preservation assertions."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils.managed import (
    MANAGED_CONFIG_FIXTURES,
    codex_state_and_agent_files,
    use_smart_routing_fixture,
)


@pytest.mark.parametrize("directory", ["arg0", "arg0-linux"])
def test_codex_snapshot_never_reads_disposable_executables(tmp_path, monkeypatch, directory):
    session = SimpleNamespace(home=tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_bytes(b"user_owned = true\n")
    helpers = config.parent / "tmp" / directory / "codex-example"
    helpers.mkdir(parents=True)
    for name in ("codex-linux-sandbox", "apply_patch", "applypatch"):
        (helpers / name).write_bytes(b"disposable native executable")

    original_read = Path.read_bytes

    def reject_executable_read(path):
        # Native helper links can each point to the same hundreds-of-MB executable.
        if path.is_relative_to(helpers):
            pytest.fail(f"snapshot tried to buffer a disposable executable: {path}")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", reject_executable_read)

    assert codex_state_and_agent_files(session) == {
        str(config.relative_to(tmp_path)): b"user_owned = true\n"
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        ".ucode/state.json",
        ".codex/config.toml",
        ".codex/auth.json",
        ".codex/tmp/keep.txt",
        ".codex/tmp/arg0-linux-other/keep.txt",
    ],
)
def test_codex_snapshot_detects_persistent_file_changes(tmp_path, relative_path):
    session = SimpleNamespace(home=tmp_path)
    before = codex_state_and_agent_files(session)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"before")

    added = codex_state_and_agent_files(session)
    key = str(path.relative_to(tmp_path))
    assert added == {**before, key: b"before"}
    path.write_bytes(b"after!")
    changed = codex_state_and_agent_files(session)
    assert changed == {**before, key: b"after!"}
    assert changed != added
    path.unlink()
    assert codex_state_and_agent_files(session) == before


def test_codex_snapshot_excludes_only_expected_control_plane_cache(tmp_path):
    session = SimpleNamespace(home=tmp_path)
    (tmp_path / ".ucode").mkdir(exist_ok=True)
    for name in ("refresh_ts.json", "managed-config.json", "state.json"):
        (tmp_path / ".ucode" / name).write_bytes(b"state")

    assert codex_state_and_agent_files(session) == {str(Path(".ucode/state.json")): b"state"}


US_WORKSPACE = "https://eng-ml-inference-team-us-east-1.cloud.databricks.com"
CA_WORKSPACE = "https://eng-ml-inference-team-ca-central-1.cloud.databricks.com"


@pytest.mark.parametrize(
    ("agent", "workspace", "smart_routing", "basename"),
    [
        ("claude", US_WORKSPACE, True, "claude_smart_routing"),
        ("claude", US_WORKSPACE, False, "claude_smart_routing_off"),
        ("claude", CA_WORKSPACE, True, "claude_smart_routing"),
        ("claude", CA_WORKSPACE, False, "claude_smart_routing_off"),
        ("codex", US_WORKSPACE, True, "codex_smart_routing"),
        ("codex", CA_WORKSPACE, False, "codex_smart_routing_ca_off"),
    ],
)
def test_smart_routing_fixture_resolves_basename_per_workspace(
    monkeypatch, agent, workspace, smart_routing, basename
):
    monkeypatch.delenv("UCODE_TEST_CODEX_ROUTING_FIXTURE", raising=False)
    session = SimpleNamespace(env={})

    path = use_smart_routing_fixture(session, agent, workspace, smart_routing=smart_routing)

    assert path == MANAGED_CONFIG_FIXTURES / f"{basename}.json"
    assert path.is_file()
    assert session.env["UCODE_MANAGED_CONFIG_STUB"] == str(path)
