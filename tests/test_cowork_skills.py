"""Component coverage for downloaded skills reaching the Cowork plugin."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ucode import cli, config_io, skills_download, state
from ucode.agents.desktop import cowork
from ucode.skills_api import SkillRef
from ucode.skills_state import SkillInstall, record_downloads

WORKSPACE = "https://workspace.example"
SKILL_FILES = {
    "SKILL.md": b"---\nname: review\ndescription: Review changes\n---\nReview changes carefully.\n",
    "references/checklist.md": b"Check error handling.\n",
}


@pytest.fixture
def cowork_home(tmp_path, monkeypatch):
    """Isolate app data; substitute remote skill APIs and the native Claude CLI only."""
    home = tmp_path / "home"
    home.mkdir()
    desktop_root = home / "Claude-3p"
    (desktop_root / "local-agent-mode-sessions" / "account" / "org").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(state, "APP_DIR", config_io.APP_DIR)
    state.save_state({"workspace": WORKSPACE, "profile": "chosen", "available_tools": ["claude"]})
    monkeypatch.setattr(cowork, "claude_desktop_root_directory", lambda: desktop_root)
    monkeypatch.setattr(cowork.shutil, "which", lambda name: str(home / "bin" / name))

    def run(argv, **kwargs):
        stdout = "[]" if argv[1:4] == ["plugin", "list", "--cowork"] else '{"outcome":"ok"}'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cowork.subprocess_cross_os, "run", run)
    monkeypatch.setattr(cli, "install_databricks_cli", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    skill = SkillRef("main", "default", "review", "review")
    monkeypatch.setattr(skills_download, "get_databricks_token", lambda *args: "token")
    monkeypatch.setattr(
        skills_download, "setup_mcp_clients", lambda *args, **kwargs: (WORKSPACE, "chosen", [])
    )
    monkeypatch.setattr(skills_download, "get_skill", lambda *args: skill)
    monkeypatch.setattr(skills_download, "list_schema_skills", lambda *args: ([skill], None))
    monkeypatch.setattr(
        skills_download, "fetch_skill_bundle", lambda *args: (dict(SKILL_FILES), None)
    )
    monkeypatch.setattr(
        skills_download, "prompt_for_skill_download_choices", lambda *args: [skill.fqn]
    )
    monkeypatch.setattr(skills_download, "prompt_yes_no", lambda *args: True)
    # MCP registration and usage reports are separate from copying downloaded skill files.
    monkeypatch.setattr(
        skills_download, "register_schemaless_skills_connection", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(skills_download, "report_skill_usage_in_background", lambda *args: None)
    return home


def _plugin_root() -> Path:
    return config_io.APP_DIR / cowork.MARKETPLACE_DIRECTORY_NAME / "plugins" / cowork.PLUGIN_NAME


def _record_skill(base: Path, name: str, *, scope: str, workspace: str = WORKSPACE) -> None:
    directory = base / ".claude" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(name, encoding="utf-8")
    record_downloads(
        [
            SkillInstall(
                fqn=f"main.default.{name}",
                bundle_name=name,
                workspace=workspace,
                scope=scope,
                base=str(base),
                dirs=(str(directory),),
            )
        ]
    )


def test_configure_copies_only_current_workspace_home_skills(cowork_home):
    _record_skill(cowork_home, "managed", scope="managed")
    _record_skill(cowork_home, "personal", scope="user", workspace=f"{WORKSPACE}/")
    _record_skill(cowork_home, "foreign", scope="user", workspace="https://other.example")
    _record_skill(cowork_home, "project_at_home", scope="project")
    _record_skill(cowork_home / "project", "project", scope="project")
    _record_skill(cowork_home / "elsewhere", "different_base", scope="user")
    authored = cowork_home / ".claude" / "skills" / "authored"
    authored.mkdir()
    (authored / "SKILL.md").write_text("user authored", encoding="utf-8")

    assert cowork.configure_cowork(state.load_state(), [])

    skills = _plugin_root() / "skills"
    assert {path.name for path in skills.iterdir()} == {"managed", "personal"}
    assert (skills / "personal" / "SKILL.md").read_text() == "personal"


@pytest.mark.parametrize(
    ("add_args", "remove_args"),
    [
        (["--names", "main.default.review"], ["--names", "main.default.review"]),
        (["--location", "main.default"], ["--location", "main.default"]),
        ([], []),
    ],
    ids=["named", "schema", "picker"],
)
def test_skills_add_update_remove_refreshes_cowork(cowork_home, monkeypatch, add_args, remove_args):
    _record_skill(cowork_home, "managed", scope="managed")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["skills", "add", *add_args])
    assert result.exit_code == 0, result.output
    skill = _plugin_root() / "skills" / "review"
    assert (skill / "SKILL.md").read_bytes() == SKILL_FILES["SKILL.md"]
    assert (skill / "references" / "checklist.md").read_bytes() == SKILL_FILES[
        "references/checklist.md"
    ]

    monkeypatch.setattr(
        skills_download, "fetch_skill_bundle", lambda *args: ({"SKILL.md": b"updated"}, None)
    )
    result = runner.invoke(cli.app, ["skills", "add", *add_args])
    assert result.exit_code == 0, result.output
    assert (skill / "SKILL.md").read_bytes() == b"updated"
    assert not (skill / "references").exists()

    monkeypatch.setattr(
        skills_download,
        "scrolling_checkbox",
        lambda *args, **kwargs: SimpleNamespace(ask=lambda: [0]),
    )
    result = runner.invoke(cli.app, ["skills", "remove", *remove_args])
    assert result.exit_code == 0, result.output
    assert {path.name for path in (_plugin_root() / "skills").iterdir()} == {"managed"}
    assert (cowork_home / ".claude" / "skills" / "managed" / "SKILL.md").read_text() == "managed"


def test_project_skill_changes_keep_cowork_home_skills(cowork_home):
    _record_skill(cowork_home, "managed", scope="managed")
    cowork.configure_cowork(state.load_state(), [])
    project = cowork_home / "project"
    project.mkdir()

    runner = CliRunner()
    for action in ("add", "remove"):
        result = runner.invoke(
            cli.app,
            ["skills", action, "--names", "main.default.review", "--path", str(project)],
        )
        assert result.exit_code == 0, result.output
        assert {path.name for path in (_plugin_root() / "skills").iterdir()} == {"managed"}
        assert (_plugin_root() / "skills" / "managed" / "SKILL.md").read_text() == "managed"


@pytest.mark.parametrize("limit_to_home", [False, True], ids=["all-bases", "home-path"])
def test_removing_last_user_skill_clears_plugin(cowork_home, limit_to_home):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["skills", "add", "--names", "main.default.review"])
    assert result.exit_code == 0, result.output
    assert (_plugin_root() / "skills" / "review" / "SKILL.md").read_bytes() == SKILL_FILES[
        "SKILL.md"
    ]

    path_args = ["--path", str(cowork_home)] if limit_to_home else []
    result = runner.invoke(
        cli.app, ["skills", "remove", "--names", "main.default.review", *path_args]
    )
    assert result.exit_code == 0, result.output
    assert not (_plugin_root() / "skills").exists()
    assert (_plugin_root() / ".claude-plugin" / "plugin.json").is_file()
    assert not (cowork_home / ".claude" / "skills" / "review").exists()


def test_failed_download_preserves_cowork_plugin(cowork_home, monkeypatch):
    _record_skill(cowork_home, "existing", scope="user")
    cowork.configure_cowork(state.load_state(), [])
    manifest = _plugin_root() / ".claude-plugin" / "plugin.json"
    modified = manifest.stat().st_mtime_ns

    def fail_auth(*args):
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(skills_download, "get_databricks_token", fail_auth)
    result = CliRunner().invoke(cli.app, ["skills", "add", "--names", "main.default.review"])

    assert result.exit_code == 1
    assert "authentication failed" in result.output
    assert manifest.stat().st_mtime_ns == modified
    assert (_plugin_root() / "skills" / "existing" / "SKILL.md").read_text() == "existing"


def test_missing_claude_cli_warns_without_failing_download(cowork_home, monkeypatch):
    monkeypatch.setattr(cowork.shutil, "which", lambda name: None)

    result = CliRunner().invoke(cli.app, ["skills", "add", "--names", "main.default.review"])

    assert result.exit_code == 0, result.output
    assert "Could not configure Claude Cowork" in result.output
    assert (cowork_home / ".claude" / "skills" / "review" / "SKILL.md").read_bytes() == SKILL_FILES[
        "SKILL.md"
    ]
