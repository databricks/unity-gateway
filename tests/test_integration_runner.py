from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import run_integration as runner


def windows_live_environment():
    return {"DATABRICKS_BEARER": "bearer"}


def test_secrets_are_redacted_from_build_output():
    output = "failed for bearer-a with token-b and token-c"

    assert (
        runner.redact_secrets(output, ("bearer-a", "token-b", "token-c"))
        == "failed for <redacted> with <redacted> and <redacted>"
    )


def test_headless_only_selects_exact_nodes_for_requested_agents(tmp_path):
    assert runner.integration_test_targets(
        tmp_path,
        ["claude", "codex"],
        platform_name="nt",
        installation_only=False,
        headless_only=True,
    ) == [
        f"{tmp_path / 'test_ug_claude_headless.py'}::test_ug_claude_headless_prompt_argument",
        f"{tmp_path / 'test_ug_codex_headless.py'}::test_ug_codex_headless_prompt_argument",
    ]


def test_copilot_headless_target_and_exact_version(tmp_path):
    assert runner.integration_test_targets(
        tmp_path,
        ["copilot"],
        platform_name="posix",
        installation_only=False,
        headless_only=True,
    ) == [
        f"{tmp_path / 'test_ug_copilot_headless.py'}::test_ug_copilot_headless_completes_file_task"
    ]
    args = runner.arguments(["--copilot-version", "1.0.91", "--installation-only"], environment={})
    assert args.copilot_version == "1.0.91"


def test_antigravity_build_selects_its_headless_node_and_version(tmp_path):
    assert runner.integration_test_targets(
        tmp_path,
        ["antigravity"],
        platform_name="posix",
        installation_only=False,
        headless_only=True,
    ) == [
        f"{tmp_path / 'test_ug_antigravity_headless.py'}"
        "::test_ug_antigravity_headless_completes_file_task"
    ]
    args = runner.arguments(
        ["--antigravity-build", "1.4.9-4962164969177088", "--installation-only"], environment={}
    )
    assert args.antigravity_build == "1.4.9-4962164969177088"
    assert args.antigravity_version == "1.4.9"


@pytest.mark.parametrize("build", ["1.4.9", "latest", "1.4.9-abc", "v1.4.9-1"])
def test_antigravity_build_must_be_exact(build):
    with pytest.raises(SystemExit):
        runner.arguments(["--antigravity-build", build, "--installation-only"], environment={})


@pytest.mark.parametrize(
    ("system", "machine", "path"),
    [
        ("linux", "x86_64", "linux-x64/cli_linux_x64.tar.gz"),
        ("linux", "aarch64", "linux-arm/cli_linux_arm64.tar.gz"),
        ("darwin", "arm64", "darwin-arm/cli_mac_arm64.tar.gz"),
    ],
)
def test_antigravity_archive_url_matches_googles_release_layout(system, machine, path):
    # Captured from the release manifests for 1.4.9.
    assert runner.antigravity_archive_url("1.4.9-4962164969177088", system, machine) == (
        "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
        f"1.4.9-4962164969177088/{path}"
    )


def _archive(tmp_path, member_name="antigravity"):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = b"#!/bin/sh\necho 1.4.9\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _serve(monkeypatch, payload):
    import io

    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(payload))
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")


def test_antigravity_install_checks_the_pinned_sha512_before_extracting(tmp_path, monkeypatch):
    import hashlib

    payload = _archive(tmp_path)
    _serve(monkeypatch, payload)
    digest = hashlib.sha512(payload).hexdigest()

    installed = runner.install_native_agent(
        "antigravity", "1.4.9-4962164969177088", tmp_path / "bin", sha512=digest
    )

    assert installed["sha512"] == digest
    assert (tmp_path / "bin/agy").read_bytes().startswith(b"#!/bin/sh")


def test_antigravity_install_rejects_a_swapped_archive(tmp_path, monkeypatch):
    # The real pinned digest cannot match this stand-in archive.
    _serve(monkeypatch, _archive(tmp_path))

    with pytest.raises(RuntimeError, match="expected 96a259b7"):
        runner.install_native_agent("antigravity", "1.4.9-4962164969177088", tmp_path / "bin")

    assert not (tmp_path / "bin/agy").exists()


def test_antigravity_install_reports_a_missing_binary(tmp_path, monkeypatch):
    import hashlib

    payload = _archive(tmp_path, member_name="other")
    _serve(monkeypatch, payload)

    with pytest.raises(RuntimeError, match="has no antigravity binary"):
        runner.install_native_agent(
            "antigravity",
            "1.4.9-4962164969177088",
            tmp_path / "bin",
            sha512=hashlib.sha512(payload).hexdigest(),
        )


def test_antigravity_unpinned_build_needs_an_explicit_sha512():
    with pytest.raises(RuntimeError, match="--antigravity-sha512"):
        runner.antigravity_expected_sha512("1.5.0-1", "linux", "x86_64")
    assert runner.antigravity_expected_sha512("1.5.0-1", "linux", "x86_64", "AB" * 64) == (
        "ab" * 64
    )


def test_antigravity_has_no_windows_build():
    with pytest.raises(RuntimeError, match="No Antigravity CLI build"):
        runner.antigravity_archive_url("1.4.9-4962164969177088", "win32", "AMD64")


def test_bedrock_defaults_use_service_credential_fixtures_and_frontier_models():
    args = runner.arguments(["--claude-version", "2.1.290", "--installation-only"], environment={})

    assert args.claude_bedrock_provider == "main.ucode.ci_bedrock_runtime_svc_cred_filtered_models"
    assert (
        args.claude_bedrock_allow_all_provider
        == "main.ucode.ci_bedrock_runtime_svc_cred_all_models"
    )
    assert args.claude_bedrock_allow_all_model == "us.anthropic.claude-sonnet-5-5"
    assert args.claude_bedrock_mantle_provider == "main.ucode.ci_bedrock_mantle_svc_cred_claude"
    assert args.claude_bedrock_mantle_model == "anthropic.claude-opus-5-5"
    assert (
        args.codex_bedrock_runtime_provider == "main.ucode.ci_bedrock_runtime_svc_cred_all_models"
    )
    assert args.codex_bedrock_runtime_model == "us.openai.gpt-5.6-terra"
    assert args.codex_bedrock_mantle_provider == "main.ucode.ci_bedrock_mantle_svc_cred_gpt"
    assert args.codex_bedrock_mantle_model == "openai.gpt-5.6-terra"


def test_copilot_version_output_matches_the_pinned_version():
    output = "GitHub Copilot CLI 1.0.91.\nRun 'copilot update' to check for updates.\n"
    assert runner.agent_version_output("copilot", output) == "GitHub Copilot CLI 1.0.91"
    assert runner.agent_version_output("codex", "codex-cli 0.154.0") == "codex-cli 0.154.0"


def test_windows_live_runs_select_every_module_except_unsupported_ones(tmp_path):
    (tmp_path / "test_headless.py").write_text("import json\n")
    (tmp_path / "test_tui.py").write_text("from utils.terminal import AgentTerminal\n")
    (tmp_path / "test_ug_claude_tracing.py").write_text("import json\n")

    assert runner.integration_test_targets(
        tmp_path,
        ["claude"],
        platform_name="nt",
        installation_only=False,
        headless_only=False,
    ) == [
        str(tmp_path / "test_headless.py"),
        str(tmp_path / "test_tui.py"),
        *(f"--deselect={node}" for node in sorted(runner.WINDOWS_UNSUPPORTED_NODES)),
    ]
    assert runner.integration_test_targets(
        tmp_path,
        ["claude"],
        platform_name="posix",
        installation_only=False,
        headless_only=False,
    ) == [str(tmp_path)]


def test_headless_only_is_mutually_exclusive_with_installation_only():
    with pytest.raises(SystemExit):
        runner.arguments(
            [
                "--claude-version",
                "2.1.268",
                "--installation-only",
                "--headless-only",
            ],
            platform_name="nt",
            environment={},
        )


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--headless-only"], windows_live_environment()),
        (["--headless-only", "--workspace", "https://example.test"], {}),
    ],
)
def test_headless_only_requires_live_workspace_and_auth(arguments, environment):
    with pytest.raises(SystemExit):
        runner.arguments(
            ["--claude-version", "2.1.268", *arguments],
            platform_name="nt",
            environment=environment,
        )


def test_headless_only_is_allowed_on_windows_with_live_workspace_and_auth():
    args = runner.arguments(
        [
            "--claude-version",
            "2.1.268",
            "--headless-only",
            "--workspace",
            "https://example.test",
        ],
        platform_name="nt",
        environment=windows_live_environment(),
    )

    assert args.headless_only is True
    assert args.installation_only is False
    assert args.pytest_args == ["-m", "live"]


def test_windows_policy_paths_match_pinned_agent_locations():
    paths = runner.managed_policy_paths(
        ["claude", "codex"],
        {"PROGRAMDATA": "C:/ProgramData", "PROGRAMFILES": "C:/Program Files"},
        platform_name="nt",
        system_platform="win32",
    )

    assert paths == (
        Path("C:/ProgramData/OpenAI/Codex/requirements.toml"),
        Path("C:/ProgramData/OpenAI/Codex/config.toml"),
        Path("C:/Program Files/ClaudeCode/managed-settings.json"),
        Path("C:/Program Files/ClaudeCode/managed-settings.d"),
        Path("C:/Program Files/ClaudeCode/.claude/skills"),
        Path("C:/Program Files/Claude/org-plugins"),
        Path("C:/Program Files/UnityGateway/claude-desktop-mdm"),
        Path("C:/Program Files/UnityGateway/claude-managed-skills.json"),
    )


def test_windows_policy_preflight_uses_native_program_files_from_32_bit_process():
    paths = runner.managed_policy_paths(
        ["claude"],
        {"ProgramFiles": "C:/Program Files (x86)", "ProgramW6432": "C:/Program Files"},
        platform_name="nt",
    )

    assert paths
    assert all(path.is_relative_to(Path("C:/Program Files")) for path in paths)


@pytest.mark.parametrize("index", range(6))
def test_windows_policy_preflight_detects_each_existing_machine_artifact(tmp_path, index):
    paths = runner.managed_policy_paths(
        ["claude"], {"PROGRAMFILES": str(tmp_path)}, platform_name="nt"
    )
    artifact = paths[index]
    if artifact.suffix == ".json":
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}")
    else:
        artifact.mkdir(parents=True)

    assert runner.present_policy_paths(paths) == [str(artifact)]


def test_policy_preflight_detects_dangling_links(tmp_path):
    policy = tmp_path / "managed-settings.json"
    policy.symlink_to(tmp_path / "missing.json")

    assert runner.present_policy_paths([policy]) == [str(policy)]


def test_policy_preflight_reports_inspection_failure(tmp_path, monkeypatch):
    # Simulate an OS denial; unreadable policy must not be treated as absent.
    def deny_stat(self):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "lstat", deny_stat)
    with pytest.raises(RuntimeError, match="Cannot inspect machine-wide agent settings"):
        runner.present_policy_paths([tmp_path / "managed-settings.json"])


@pytest.fixture
def registry(monkeypatch):
    # The Windows registry is an external boundary unavailable on Linux test hosts.
    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=0x20019,
        KEY_WOW64_64KEY=0x100,
        OpenKey=MagicMock(),
        QueryInfoKey=MagicMock(return_value=(0, 1, 0)),
    )
    monkeypatch.setitem(runner.sys.modules, "winreg", registry)
    return registry


@pytest.mark.parametrize("counts", [(0, 1, 0), (1, 0, 0)])
def test_windows_registry_preflight_detects_values_or_subkeys_in_native_view(registry, counts):
    registry.QueryInfoKey.return_value = counts

    assert runner.present_registry_policies(["claude"], platform_name="nt") == [
        r"HKLM\SOFTWARE\Policies\Claude"
    ]
    registry.OpenKey.assert_called_once_with(
        registry.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Policies\Claude",
        0,
        registry.KEY_READ | registry.KEY_WOW64_64KEY,
    )
    registry.OpenKey.return_value.__exit__.assert_called_once()


def test_windows_registry_preflight_accepts_empty_key(registry):
    registry.QueryInfoKey.return_value = (0, 0, 0)

    assert runner.present_registry_policies(["claude"], platform_name="nt") == []


def test_windows_registry_preflight_accepts_missing_key(registry):
    registry.OpenKey.side_effect = FileNotFoundError

    assert runner.present_registry_policies(["claude"], platform_name="nt") == []


@pytest.mark.parametrize("operation", ["OpenKey", "QueryInfoKey"])
def test_windows_registry_preflight_fails_closed_on_access_denied(registry, operation):
    getattr(registry, operation).side_effect = PermissionError("access denied")

    with pytest.raises(RuntimeError, match="Cannot inspect machine-wide Desktop policy"):
        runner.present_registry_policies(["claude"], platform_name="nt")


@pytest.mark.parametrize("agents,platform_name", [(["codex"], "nt"), (["claude"], "posix")])
def test_registry_preflight_leaves_other_agents_and_platforms_alone(
    registry, agents, platform_name
):
    assert runner.present_registry_policies(agents, platform_name=platform_name) == []
    registry.OpenKey.assert_not_called()


@pytest.mark.parametrize(
    ("system_platform", "claude_path"),
    [
        ("linux", "/etc/claude-code/managed-settings.json"),
        ("darwin", "/Library/Application Support/ClaudeCode/managed-settings.json"),
    ],
)
def test_posix_policy_paths_include_the_installed_workspace_policy(system_platform, claude_path):
    paths = runner.managed_policy_paths(
        ["claude", "codex"], {}, platform_name="posix", system_platform=system_platform
    )

    assert paths == (
        Path("/etc/unitygateway/managed-settings.json"),
        Path("/etc/codex/managed_config.toml"),
        Path("/etc/codex/requirements.toml"),
        Path(claude_path),
    )


def test_windows_policy_preflight_fails_closed_without_required_roots():
    with pytest.raises(RuntimeError, match="PROGRAMDATA"):
        runner.managed_policy_paths(["codex"], {}, platform_name="nt", system_platform="win32")
    with pytest.raises(RuntimeError, match="PROGRAMFILES"):
        runner.managed_policy_paths(["claude"], {}, platform_name="nt", system_platform="win32")
