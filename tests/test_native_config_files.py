"""Native file references: local preview, caller-authorized downloads, and parsing failures."""

from __future__ import annotations

import copy
import io
from types import SimpleNamespace

import pytest

from ucode import native_config_files as files

WS = "https://example.cloud.databricks.com"
VOLUME_FILE = "/Volumes/main/ai/configs/settings.json"


def _manifest(tool="claude", **native):
    variant = "claude_code" if tool == "claude" else "codex"
    return {"enabled_agents": {tool: {variant: native}}}


def _resolve(manifest, **kwargs):
    return files.resolve_native_config_files(manifest, WS, agents=["claude", "codex"], **kwargs)


def _remote(monkeypatch, download):
    # The SDK is the external file transport. The resolver and parsers remain real.
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda **kwargs: SimpleNamespace(
            config=SimpleNamespace(workspace_id="stale"), files=SimpleNamespace(download=download)
        ),
    )


@pytest.mark.parametrize(
    ("tool", "role", "reference", "filename", "content", "expected"),
    [
        (
            "claude",
            "managed_settings",
            "managed_settings_json",
            "managed-settings.json",
            '{"cleanupPeriodDays": 14, "language": "日本語"}',
            {"cleanupPeriodDays": 14, "language": "日本語"},
        ),
        (
            "codex",
            "config",
            "config_toml",
            "config.toml",
            'file_opener = "none"\n[features]\nweb_search = false\n',
            {"file_opener": "none", "features": {"web_search": False}},
        ),
        (
            "codex",
            "requirements",
            "requirements_toml",
            "requirements.toml",
            'allowed_sandbox_modes = ["read-only", "workspace-write"]',
            {"allowed_sandbox_modes": ["read-only", "workspace-write"]},
        ),
    ],
)
def test_local_preview_resolves_relative_to_the_draft(
    tmp_path, monkeypatch, tool, role, reference, filename, content, expected
):
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    (draft_dir / filename).write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient", lambda **kw: pytest.fail("local preview authenticated")
    )
    original = _manifest(tool, **{reference: filename})
    before = copy.deepcopy(original)

    resolved = _resolve(original, source=str(draft_dir / "draft.json"))

    variant = "claude_code" if tool == "claude" else "codex"
    assert resolved["enabled_agents"][tool][variant] == {role: expected}
    assert original == before


def test_remote_files_are_refetched_at_the_same_path(monkeypatch):
    responses = iter([b'{"cleanupPeriodDays": 14}', b'{"cleanupPeriodDays": 7}'])
    paths = []

    def download(path):
        paths.append(path)
        return SimpleNamespace(contents=io.BytesIO(next(responses)))

    _remote(monkeypatch, download)
    manifest = _manifest(managed_settings_json=VOLUME_FILE)
    first = _resolve(manifest, token="test-token")
    second = _resolve(manifest, token="test-token")
    assert first["enabled_agents"]["claude"]["claude_code"]["managed_settings"] == {
        "cleanupPeriodDays": 14
    }
    assert second["enabled_agents"]["claude"]["claude_code"]["managed_settings"] == {
        "cleanupPeriodDays": 7
    }
    assert paths == [VOLUME_FILE, VOLUME_FILE]


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/Volumes/c/s/v",
        "/Volumes/c/s/v/",
        "/Volumes/c/s/v/../x",
        "/Volumes/c/s/v//x",
        "/Volumes/c/s/v/./x",
        "/Volumes/c/s/v/a\\b",
        "/Volumes/c/s/v/a\n",
        "https://other.example/settings.json",
        "local.json",
        None,
        12,
    ],
)
def test_published_paths_must_identify_a_canonical_volume_file(path, monkeypatch):
    _remote(monkeypatch, lambda path: pytest.fail("invalid reference was downloaded"))
    with pytest.raises(ValueError, match="agent_native_config.claude_code.managed_settings_json"):
        _resolve(_manifest(managed_settings_json=path), token="test-token")


@pytest.mark.parametrize(
    "tool,role,reference",
    [
        ("claude", "managed_settings", "managed_settings_json"),
        ("codex", "config", "config_toml"),
        ("codex", "requirements", "requirements_toml"),
    ],
)
def test_inline_and_file_for_one_role_are_rejected(tool, role, reference):
    with pytest.raises(ValueError, match="choose inline"):
        _resolve(_manifest(tool, **{role: {}, reference: VOLUME_FILE}))


@pytest.mark.parametrize(
    "tool,role,reference,content",
    [
        ("claude", "managed_settings", "managed_settings_json", b"[1]"),
        ("claude", "managed_settings", "managed_settings_json", b"{"),
        ("claude", "managed_settings", "managed_settings_json", b'{"a": NaN}'),
        ("claude", "managed_settings", "managed_settings_json", b"\xff"),
        ("codex", "config", "config_toml", b"[broken"),
        ("codex", "requirements", "requirements_toml", b"x = null"),
    ],
)
def test_malformed_native_files_fail_with_the_field_and_path(
    tmp_path, tool, role, reference, content
):
    native = tmp_path / "native"
    native.write_bytes(content)
    with pytest.raises(ValueError, match=f"{reference}.*native"):
        _resolve(_manifest(tool, **{reference: str(native)}), source=str(tmp_path / "draft.json"))


def test_local_file_size_is_bounded(tmp_path):
    native = tmp_path / "large.json"
    native.write_bytes(b" " * (files.MAX_NATIVE_CONFIG_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        _resolve(_manifest(managed_settings_json=str(native)), source=str(tmp_path / "draft.json"))


def test_remote_size_is_bounded_and_stream_is_closed(monkeypatch):
    stream = io.BytesIO(b" " * (files.MAX_NATIVE_CONFIG_FILE_BYTES + 1))
    _remote(monkeypatch, lambda path: SimpleNamespace(contents=stream))
    with pytest.raises(ValueError, match="exceeds"):
        _resolve(_manifest(managed_settings_json=VOLUME_FILE), token="test-token")
    assert stream.closed


def test_permission_failure_preserves_the_input(monkeypatch):
    from databricks.sdk.errors import PermissionDenied

    def denied(path):
        raise PermissionDenied("READ VOLUME is required")

    _remote(monkeypatch, denied)
    manifest = _manifest(managed_settings_json=VOLUME_FILE)
    before = copy.deepcopy(manifest)
    with pytest.raises(OSError, match="READ VOLUME"):
        _resolve(manifest, token="test-token")
    assert manifest == before


def test_missing_local_file_is_a_draft_error(tmp_path):
    with pytest.raises(ValueError, match="missing.json"):
        _resolve(
            _manifest(managed_settings_json="missing.json"), source=str(tmp_path / "draft.json")
        )


def test_unselected_agent_files_are_not_read():
    result = files.resolve_native_config_files(
        _manifest(managed_settings_json=VOLUME_FILE), WS, agents=["codex"]
    )
    assert result == _manifest(managed_settings_json=VOLUME_FILE)


def test_inline_settings_and_a_different_file_role_can_coexist(tmp_path):
    native = tmp_path / "requirements.toml"
    native.write_text('allowed_approval_policies = ["on-request"]')
    manifest = _manifest("codex", config={"file_opener": "none"}, requirements_toml=str(native))
    result = _resolve(manifest, source=str(tmp_path / "draft.json"))
    assert result["enabled_agents"]["codex"]["codex"] == {
        "config": {"file_opener": "none"},
        "requirements": {"allowed_approval_policies": ["on-request"]},
    }
