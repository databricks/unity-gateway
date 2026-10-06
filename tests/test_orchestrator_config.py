"""Exercise real CLI configuration, ownership, and removal without model calls."""

import errno
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.smart_routing import session_env

# Only replace the external machine config path; run the real helper and file operations.
CONFIG_CLI = """
import runpy
import sys
from pathlib import Path
from ucode import codex_config
script, managed, *arguments = sys.argv[1:]
codex_config.codex_managed_config_path = lambda: Path(managed)
sys.argv = [script, *arguments]
runpy.run_path(script, run_name="__main__")
"""


class ConfigHarness:
    def __init__(self, root: Path, script: Path):
        self.root = root
        self.script = script
        self.project = root / "project with spaces"
        self.project.mkdir()
        self.env = dict(
            os.environ,
            XDG_CONFIG_HOME=str(root / "config"),
            CLAUDE_CONFIG_DIR=str(root / "claude"),
            CODEX_HOME=str(root / "codex-home"),
        )
        self.env.pop("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", None)
        self.env["ENABLE_SMART_ROUTING_V2"] = "1"
        self.env.pop("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", None)
        session_env.start_session(self.env)
        self.env.pop("ISAAC_LAUNCH_MODE", None)

    @property
    def config(self):
        return self.project / ".model-orchestrator.json"

    @property
    def journal(self):
        return self.config.with_name(self.config.name + ".transaction.json")

    @property
    def user_config(self):
        return Path(self.env["XDG_CONFIG_HOME"]) / "model-orchestrator/config.json"

    def cli(self, *args, user=False, ok=True) -> Any:
        scope = ["--user"] if user else ["--project", str(self.project)]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                CONFIG_CLI,
                str(self.script),
                str(self.root / "managed.toml"),
                *args,
                *scope,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert (result.returncode == 0) == ok, result.stderr
        return json.loads(result.stdout) if ok else result.stderr

    def set_model(self, harness="claude", role="worker", model="provider/custom-model", **kwargs):
        return self.cli("set", "--harness", harness, "--role", role, "--model", model, **kwargs)

    def set_catalog(self, *models):
        catalog = self.root / "codex-model-catalog.json"
        catalog.write_text(json.dumps({"models": [{"slug": model} for model in models]}))
        codex_home = Path(self.env["CODEX_HOME"])
        codex_home.mkdir(exist_ok=True)
        (codex_home / "ucode.config.toml").write_text(
            f"model_catalog_json = {json.dumps(str(catalog))}\n"
        )
        return catalog

    def agent(self, role="worker", user=False):
        root = Path(self.env["CLAUDE_CONFIG_DIR"]) if user else self.project / ".claude"
        return (
            root / "agents" / f"model-orchestrator-custom-{'user' if user else 'project'}-{role}.md"
        )

    def interrupt(self, target, *arguments):
        program = """
import importlib.util
import os
from pathlib import Path
import sys

script, target, *arguments = sys.argv[1:]
spec = importlib.util.spec_from_file_location("orchestrator_configure", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original_replace = os.replace
original_unlink = os.unlink

def replace(source, destination):
    original_replace(source, destination)
    if Path(destination) == Path(target):
        os._exit(17)

def unlink(destination, *args, **kwargs):
    original_unlink(destination, *args, **kwargs)
    if Path(destination) == Path(target):
        os._exit(17)

os.replace = replace
os.unlink = unlink
sys.argv = [script, *arguments]
module.main()
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(self.script),
                str(target),
                *arguments,
                "--project",
                str(self.project),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 17, result.stderr


@pytest.fixture
def config(tmp_path):
    return ConfigHarness(
        tmp_path, Path(__file__).parents[1] / "skills/orchestrate/scripts/configure.py"
    )


@pytest.fixture
def configure_module(config, monkeypatch):
    spec = importlib.util.spec_from_file_location("orchestrator_configure", config.script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "codex_managed_config_path", lambda: config.root / "managed.toml")
    for key in (session_env.SESSION_ENV_VAR, "ENABLE_SMART_ROUTING_V2"):
        monkeypatch.setenv(key, config.env[key])
    return module


def test_defaults_need_no_files(config):
    claude = config.cli("show", "--harness", "claude")
    codex = config.cli("show", "--harness", "codex")
    assert {choice["model"] for choice in claude.values()} == {"sonnet"}
    assert claude["reviewer"]["subagent_type"] == "ug-smart-router:reviewer"
    assert codex["worker"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "allow_inherited_fallback": True,
    }
    assert list(config.project.iterdir()) == []
    assert not Path(config.env["XDG_CONFIG_HOME"]).exists()


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("state", ["off", "no-session"])
def test_show_never_authorizes_delegation_when_routing_is_off(config, harness, configured, state):
    if configured:
        config.set_model(harness=harness)
    if state == "off":
        Path(config.env[session_env.SESSION_ENV_VAR]).write_text(
            '{"ENABLE_SMART_ROUTING_V2":"0","ENABLE_SMART_ROUTING_SUBAGENT_ONLY":"0"}'
        )
    else:
        config.env.pop(session_env.SESSION_ENV_VAR)
    error = config.cli("show", "--harness", harness, ok=False)
    assert "do not start new automatic delegation" in error
    # Preferences can still be managed without enabling either feature.
    config.set_model(harness=harness)
    config.cli("unconfigure")


def test_codex_managed_catalog_precedes_ug_and_user_catalogs(config):
    catalog = config.set_catalog("gpt-5.6-luna")
    managed_catalog = config.root / "managed-models.json"
    managed_catalog.write_text('{"models":[{"slug":"system.ai.gpt-5-6-luna"}]}')
    (config.root / "managed.toml").write_text(
        f"model_catalog_json = {json.dumps(str(managed_catalog))}\n"
    )
    (Path(config.env["CODEX_HOME"]) / "config.toml").write_text(
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
    )
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == ("system.ai.gpt-5-6-luna")


@pytest.mark.parametrize(
    "configured,equivalent",
    [
        ("gpt-5.6-luna", "system.ai.gpt-5-6-luna"),
        ("system.ai.gpt-5-6-luna", "gpt-5.6-luna"),
        ("gpt-6-luna", "system.ai.gpt-6-luna"),
        ("system.ai.gpt-6-sol", "gpt-6-sol"),
        ("glm-5-3", "system.ai.glm-5-3"),
        ("system.ai.deepseek-v4-1-flash", "deepseek-v4-1-flash"),
    ],
)
def test_codex_catalog_aliases_resolve_to_an_exact_available_model(config, configured, equivalent):
    config.set_model(harness="codex", model=configured)
    config.set_catalog(equivalent)
    worker = config.cli("show", "--harness", "codex")["worker"]
    assert worker == {
        "model": equivalent,
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }


def test_codex_catalog_prefers_the_configured_spelling(config):
    config.set_model(harness="codex", model="gpt-5.6-luna")
    config.set_catalog("system.ai.gpt-5-6-luna", "gpt-5.6-luna")
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "gpt-5.6-luna"


def test_codex_full_model_id_is_not_rewritten(config):
    config.set_model(harness="codex", model="provider/custom-model")
    assert config.cli("show", "--harness", "codex")["worker"] == {
        "model": "provider/custom-model",
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }


@pytest.mark.parametrize(
    "model",
    [
        "provider/custom-model",
        "provider:custom-model",
        "system.ai.provider/custom-model",
        "system.ai.provider:custom-model",
    ],
)
def test_codex_custom_model_id_bypasses_catalog_alias_matching(config, model):
    config.set_catalog("system.ai.gpt-5-6-luna", "provider/custom-model", "provider:custom-model")
    config.set_model(harness="codex", model=model)
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == model


@pytest.mark.parametrize(
    "configured,other_version",
    [
        ("gpt-6-luna", "gpt-5.6-luna"),
        ("gpt-5.6-luna", "gpt-6-luna"),
        ("gpt-6-sol", "gpt-5.6-sol"),
        ("gpt-5.6-sol", "gpt-6-sol"),
    ],
)
def test_codex_catalog_aliases_do_not_cross_model_versions(config, configured, other_version):
    config.set_model(harness="codex", model=configured)
    config.set_catalog(other_version)
    assert config.cli("show", "--harness", "codex")["worker"] == {
        "model": configured,
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }


@pytest.mark.parametrize("available", [True, False])
def test_codex_catalog_preserves_bundled_fallback_eligibility(config, available):
    model = "system.ai.gpt-5-6-luna" if available else "another-model"
    config.set_catalog(model)
    roles = config.cli("show", "--harness", "codex")
    assert set(roles) == {"explorer", "researcher", "worker", "tester", "reviewer"}
    for choice in roles.values():
        assert choice == {
            "model": model if available else "gpt-5.6-luna",
            "reasoning_effort": "max",
            "allow_inherited_fallback": True,
        }


def test_codex_unavailable_role_does_not_block_an_available_role(config):
    config.set_model(harness="codex", role="explorer", model="missing-model")
    config.set_model(harness="codex", role="reviewer", model="glm-5-3")
    config.set_catalog("system.ai.glm-5-3")
    roles = config.cli("show", "--harness", "codex")
    assert roles["explorer"] == {
        "model": "missing-model",
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }
    assert roles["reviewer"] == {
        "model": "system.ai.glm-5-3",
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }
    assert roles["worker"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "allow_inherited_fallback": True,
    }


def test_codex_catalog_rejects_missing_or_malformed_catalog(config):
    missing = config.set_catalog("system.ai.gpt-5-6-luna")
    missing.unlink()
    assert str(missing) in config.cli("show", "--harness", "codex", ok=False)
    missing.write_text("not json")
    assert str(missing) in config.cli("show", "--harness", "codex", ok=False)


@pytest.mark.parametrize(
    "contents", ["[]", "{}", '{"models": []}', '{"models": [null]}', '{"models": [{"slug": 5}]}']
)
def test_codex_catalog_rejects_invalid_structure(config, contents):
    catalog = config.set_catalog("system.ai.gpt-5-6-luna")
    catalog.write_text(contents)
    assert str(catalog) in config.cli("show", "--harness", "codex", ok=False)


def test_codex_catalog_falls_back_to_codex_config(config):
    catalog = config.set_catalog("system.ai.gpt-5-6-luna")
    codex_home = config.root / "codex-home"
    (codex_home / "ucode.config.toml").unlink()
    (codex_home / "config.toml").write_text(f"model_catalog_json = {json.dumps(str(catalog))}\n")
    config.env["CODEX_HOME"] = str(codex_home)
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "system.ai.gpt-5-6-luna"


def test_codex_ug_catalog_takes_precedence_over_user_config(config):
    config.set_catalog("system.ai.gpt-5-6-luna")
    codex_home = Path(config.env["CODEX_HOME"])
    codex_home.mkdir(exist_ok=True)
    (codex_home / "config.toml").write_text('model_catalog_json = "missing.json"\n')
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "system.ai.gpt-5-6-luna"


def test_codex_catalog_config_relative_path(config):
    codex_home = Path(config.env["CODEX_HOME"])
    codex_home.mkdir(exist_ok=True)
    (codex_home / "models.json").write_text('{"models": [{"slug": "system.ai.gpt-5-6-luna"}]}')
    (codex_home / "config.toml").write_text('model_catalog_json = "models.json"\n')
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "system.ai.gpt-5-6-luna"


@pytest.mark.parametrize(
    "contents", ["model_catalog_json = 5", 'model_catalog_json = ""', "not toml"]
)
def test_codex_catalog_rejects_invalid_config(config, contents):
    codex_home = Path(config.env["CODEX_HOME"])
    codex_home.mkdir(exist_ok=True)
    config_path = codex_home / "config.toml"
    config_path.write_text(contents)
    assert str(config_path) in config.cli("show", "--harness", "codex", ok=False)


def test_codex_catalog_does_not_hide_invalid_preferences(config):
    config.set_catalog("system.ai.gpt-5-6-luna")
    config.config.write_text('{"codex": {"worker": {"model": ""}}}')
    assert "Invalid model for codex/worker" in config.cli("show", "--harness", "codex", ok=False)


@pytest.mark.parametrize("catalog", [None, "system.ai.gpt-5-6-luna", "another-model"])
@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("model", ["gpt-5.6-luna", "provider/custom-model"])
def test_codex_inherited_fallback_preserves_explicit_model_choices(config, user, model, catalog):
    if catalog is not None:
        config.set_catalog(catalog)
    config.set_model(harness="codex", model=model, user=user)
    roles = config.cli("show", "--harness", "codex")
    expected = catalog if model == "gpt-5.6-luna" and catalog == "system.ai.gpt-5-6-luna" else model
    assert roles["worker"]["model"] == expected
    assert roles["worker"]["allow_inherited_fallback"] is False
    assert roles["reviewer"]["allow_inherited_fallback"] is True
    config.cli("unconfigure", user=user)
    assert config.cli("show", "--harness", "codex")["worker"]["allow_inherited_fallback"] is True


def test_codex_fallback_stays_disabled_when_project_override_reveals_user_choice(config):
    config.set_model(harness="codex", model="user-model", user=True)
    config.set_model(harness="codex", model="project-model")
    worker = config.cli("show", "--harness", "codex")["worker"]
    assert worker["model"] == "project-model"
    assert worker["allow_inherited_fallback"] is False
    config.cli("unconfigure")
    worker = config.cli("show", "--harness", "codex")["worker"]
    assert worker["model"] == "user-model"
    assert worker["allow_inherited_fallback"] is False


@pytest.mark.parametrize("role", ["explorer", "researcher", "worker", "tester", "reviewer"])
def test_bundled_claude_agents_use_sonnet(config, role):
    agent = config.script.parents[1] / "agents" / f"{role}.md"
    frontmatter = agent.read_text().split("---", 2)[1]
    assert "model: sonnet" in frontmatter.splitlines()


def test_bundled_skill_inherits_supervisor_model(config):
    skill = config.script.parents[1] / "SKILL.md"
    frontmatter = skill.read_text().split("---", 2)[1]
    assert "model: inherit" in frontmatter.splitlines()


def test_full_id_effort_idempotence_and_cleanup(config):
    arguments = (
        "set",
        "--harness",
        "claude",
        "--role",
        "worker",
        "--model",
        "provider/model:#id",
        "--effort",
        "high",
    )
    config.cli(*arguments)
    agent = config.agent()
    assert 'model: "provider/model:#id"\neffort: high' in agent.read_text()
    stamp = agent.stat().st_mtime_ns
    assert config.cli(*arguments)["changed"] == []
    assert agent.stat().st_mtime_ns == stamp
    assert config.cli("show", "--harness", "claude")["worker"] == {
        "model": "provider/model:#id",
        "effort": "high",
        "subagent_type": "model-orchestrator-custom-project-worker",
    }
    config.cli("unconfigure")
    assert not agent.exists()
    assert not config.config.exists()
    assert not config.journal.exists()


def test_project_overrides_user_and_harnesses_stay_separate(config):
    config.set_model(model="user-model", user=True)
    config.set_model(model="project-model")
    config.set_model(harness="codex", role="reviewer", model="other-model")
    assert config.cli("show", "--harness", "claude")["worker"]["model"] == "project-model"
    original = config.agent(user=True).read_bytes()
    config.agent(user=True).write_text("Edited but shadowed by project override")
    assert config.cli("show", "--harness", "claude")["worker"]["model"] == "project-model"
    config.agent(user=True).write_bytes(original)
    assert config.cli("show", "--harness", "codex")["reviewer"] == {
        "model": "other-model",
        "reasoning_effort": None,
        "allow_inherited_fallback": False,
    }
    config.cli("unconfigure")
    assert config.cli("show", "--harness", "claude")["worker"]["model"] == "user-model"
    config.cli("unconfigure", "--harness", "claude", user=True, ok=False)
    assert config.agent(user=True).exists()


def test_edited_and_unowned_agents_are_preserved(config):
    config.set_model()
    agent = config.agent()
    original = agent.read_text()
    agent.write_text(original + "User edit\n")
    before = config.config.read_bytes()
    assert "Preserving" in config.cli("unconfigure", ok=False)
    assert config.config.read_bytes() == before
    assert agent.read_text().endswith("User edit\n")
    assert "Missing/stale" in config.cli("show", "--harness", "claude", ok=False)
    agent.write_text(original)
    config.cli("unconfigure")
    agent.write_text("Unrelated file\n")
    assert "Preserving" in config.set_model(ok=False)
    assert agent.read_text() == "Unrelated file\n"
    assert not config.config.exists()


@pytest.mark.parametrize("model", ["", "bad\nmodel", "bad model", "bad\x01model"])
def test_invalid_models_do_not_write(config, model):
    config.set_model(model=model, ok=False)
    assert not config.config.exists()
    assert not config.agent().exists()


def test_invalid_effort_does_not_write(config):
    config.cli(
        "set",
        "--harness",
        "claude",
        "--role",
        "worker",
        "--model",
        "sonnet",
        "--effort",
        "unsupported-effort",
        ok=False,
    )
    assert not config.config.exists()
    assert not config.agent().exists()


@pytest.mark.parametrize("location", ["project", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR"])
def test_symlinked_scope_roots_are_supported(config, location):
    alias = config.root / "alias"
    if location == "project":
        alias.symlink_to(config.project, target_is_directory=True)
        config.project = alias
    else:
        target = Path(config.env[location])
        target.mkdir()
        alias.symlink_to(target, target_is_directory=True)
        config.env[location] = str(alias)
    user = location != "project"
    config.set_model(user=user)
    assert (
        config.cli("show", "--harness", "claude", user=user)["worker"]["model"]
        == "provider/custom-model"
    )
    config.cli("unconfigure", user=user)
    assert not config.agent(user=user).exists()


@pytest.mark.parametrize(
    "location",
    [
        ".claude",
        ".model-orchestrator.json",
        ".model-orchestrator.json.lock",
        ".model-orchestrator.json.transaction.json",
    ],
)
def test_managed_symlinks_are_rejected(config, location):
    outside = config.root / "outside"
    if location == ".claude":
        outside.mkdir()
    else:
        outside.write_text("Untouched")
    (config.project / location).symlink_to(outside)
    assert "symlink" in config.set_model(ok=False)
    if outside.is_dir():
        assert list(outside.iterdir()) == []
    else:
        assert outside.read_text() == "Untouched"


@pytest.mark.parametrize(
    "data,harness",
    [
        ([], "claude"),
        ({"unknown": {}}, "claude"),
        ({"codex": {"unknown": {}}}, "codex"),
        ({"_generated": {"worker": "bad"}}, "claude"),
    ],
)
def test_malformed_configuration_is_rejected(config, data, harness):
    config.config.write_text(json.dumps(data))
    config.cli("show", "--harness", harness, ok=False)


def test_forced_model_policy_is_preserved(config):
    config.env.update(CLAUDE_CODE_SUBAGENT_MODEL="opus", CLAUDE_CODE_SUBAGENT_MODEL_FORCE="1")
    assert "conflicts" in config.cli("show", "--harness", "claude", ok=False)
    config.env.pop("CLAUDE_CODE_SUBAGENT_MODEL")
    assert "parent model" in config.cli("show", "--harness", "claude", ok=False)
    config.cli("show", "--harness", "codex")


def test_codex_update_preserves_edited_claude_agents(config):
    config.set_model()
    config.agent().write_text("User customization\n")
    receipts = json.loads(config.config.read_text())["_generated"]
    config.set_model(harness="codex", model="codex-model")
    assert config.agent().read_text() == "User customization\n"
    assert json.loads(config.config.read_text())["_generated"] == receipts
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "codex-model"
    assert "Preserving" in config.cli("unconfigure", ok=False)


@pytest.mark.parametrize(
    "section,value",
    [
        ("claude", {"worker": {"model": "invalid model"}}),
        ("claude", []),
        ("claude", {"unknown": None}),
        ("_generated", {"worker": "bad"}),
        ("_generated", []),
        ("_generated", None),
    ],
)
def test_codex_update_preserves_malformed_claude_state(config, section, value):
    config.set_model()
    agent_before = config.agent().read_bytes()
    data = json.loads(config.config.read_text())
    data[section] = value
    config.config.write_text(json.dumps(data))
    config.set_model(harness="codex", model="codex-model")
    updated = json.loads(config.config.read_text())
    assert updated["claude"] == data["claude"]
    assert updated["_generated"] == data["_generated"]
    assert config.agent().read_bytes() == agent_before
    assert config.cli("show", "--harness", "codex")["worker"]["model"] == "codex-model"


def test_codex_set_repairs_only_the_selected_role(config):
    config.config.write_text(json.dumps({"codex": {"worker": {"model": 4}, "reviewer": []}}))
    config.set_model(harness="codex", model="codex-model")
    updated = json.loads(config.config.read_text())
    assert updated["codex"] == {"worker": {"model": "codex-model", "effort": None}, "reviewer": []}


@pytest.mark.parametrize(
    "section,value",
    [
        ("claude", {"worker": {"model": 4}}),
        ("claude", []),
        ("claude", {"unknown": {}}),
        ("codex", {"worker": {"model": "invalid model"}}),
        ("codex", []),
    ],
)
def test_unconfigure_uses_ownership_receipts_despite_invalid_roles(config, section, value):
    config.set_model()
    unrelated = config.agent(role="reviewer")
    unrelated.write_text("Unrelated file\n")
    data = json.loads(config.config.read_text())
    data[section] = value
    config.config.write_text(json.dumps(data))
    config.cli("unconfigure")
    assert not config.config.exists()
    assert not config.agent().exists()
    assert unrelated.read_text() == "Unrelated file\n"


@pytest.mark.parametrize("receipts", [{"worker": "bad"}, [], None])
def test_unconfigure_preserves_files_when_ownership_is_invalid(config, receipts):
    config.set_model()
    agent_before = config.agent().read_bytes()
    data = json.loads(config.config.read_text())
    data["_generated"] = receipts
    config.config.write_text(json.dumps(data))
    config_before = config.config.read_bytes()
    assert "ownership" in config.cli("unconfigure", ok=False)
    assert config.config.read_bytes() == config_before
    assert config.agent().read_bytes() == agent_before


@pytest.mark.parametrize("action", ["set", "unconfigure"])
def test_corrupt_json_updates_and_cleanup_preserve_files(config, action):
    config.set_model()
    agent_before = config.agent().read_bytes()
    config.config.write_text("{invalid json")
    if action == "set":
        error = config.set_model(harness="codex", ok=False)
    else:
        error = config.cli("unconfigure", ok=False)
    assert "repair or restore" in error
    assert config.config.read_text() == "{invalid json"
    assert config.agent().read_bytes() == agent_before


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("role", ["explorer", "researcher", "worker", "tester", "reviewer"])
def test_template_upgrade_refreshes_only_unedited_owned_agents(config, user, edited, role):
    package = config.root / "plugin"
    shutil.copytree(config.script.parents[1], package)
    config.script = package / "scripts/configure.py"
    arguments = (
        "set",
        "--harness",
        "claude",
        "--role",
        role,
        "--model",
        "provider/custom-model",
        "--effort",
        "high",
    )
    config.cli(*arguments, user=user)
    config_path = config.user_config if user else config.config
    config_before = config_path.read_bytes()
    template = package / "agents" / f"{role}.md"
    template.chmod(template.stat().st_mode | stat.S_IWUSR)
    template.write_text(template.read_text() + "\nUpdated role instructions.\n")
    agent = config.agent(role=role, user=user)
    if edited:
        agent.write_text(agent.read_text() + "User edit\n")
    assert "Missing/stale" in config.cli("show", "--harness", "claude", user=user, ok=False)
    if edited:
        assert "Preserving" in config.cli(*arguments, user=user, ok=False)
        assert config_path.read_bytes() == config_before
        assert agent.read_text().endswith("User edit\n")
    else:
        config.cli(*arguments, user=user)
        assert agent.read_text().endswith("Updated role instructions.\n")
        assert (
            json.loads(config_path.read_text())["_generated"]
            != json.loads(config_before)["_generated"]
        )
        resolved = config.cli("show", "--harness", "claude", user=user)[role]
        assert resolved["model"] == "provider/custom-model"
        assert resolved["effort"] == "high"


@pytest.mark.parametrize(
    "choice",
    [{"model": "invalid model"}, {"model": 4}, {"model": "sonnet", "effort": "invalid"}, []],
)
def test_invalid_shadowed_role_is_ignored_but_invalid_fallback_is_rejected(config, choice):
    config.set_model(model="user-model", user=True)
    config.set_model(model="project-model")
    data = json.loads(config.user_config.read_text())
    data["claude"]["worker"] = choice
    config.user_config.write_text(json.dumps(data))
    assert config.cli("show", "--harness", "claude")["worker"]["model"] == "project-model"
    config.cli("show", "--harness", "claude", user=True, ok=False)
    config.cli("show", "--harness", "codex")


def test_corrupt_user_json_is_not_hidden_by_project_overrides(config):
    config.set_model(user=True)
    config.set_model(model="project-model")
    config.user_config.write_text("{invalid json")
    config.cli("show", "--harness", "claude", ok=False)


def test_supported_effort_and_concurrent_writer_guard(config):
    config.cli(
        "set", "--harness", "claude", "--role", "worker", "--model", "sonnet", "--effort", "xhigh"
    )
    before = config.config.read_bytes()
    lock = config.config.with_name(config.config.name + ".lock")
    with lock.open("a+b") as stream:
        acquire_exclusive_file_lock(stream, blocking=False)
        try:
            assert "busy" in config.set_model(role="reviewer", ok=False)
            assert "busy" in config.cli("show", "--harness", "claude", ok=False)
        finally:
            release_file_lock(stream)
    assert config.config.read_bytes() == before
    assert not config.agent(role="reviewer").exists()
    config.set_model(role="reviewer")
    assert set(json.loads(config.config.read_text())["claude"]) == {"worker", "reviewer"}


def test_show_reads_existing_lock_on_read_only_mount(config, configure_module, monkeypatch, capsys):
    config.cli(
        "set",
        "--harness",
        "codex",
        "--role",
        "worker",
        "--model",
        "gpt-6-luna",
        "--effort",
        "max",
        user=True,
    )
    lock = config.user_config.with_name(config.user_config.name + ".lock")
    assert lock.is_file()
    for name in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.setenv(name, config.env[name])
    original_open = Path.open

    # Reject writes to the lock file to represent a read-only configuration mount.
    def open_without_lock_writes(path, mode="r", *args, **kwargs):
        if path == lock and "a" in mode:
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_without_lock_writes)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(config.script), "show", "--harness", "codex", "--project", str(config.project)],
    )
    configure_module.main()
    assert json.loads(capsys.readouterr().out)["worker"] == {
        "model": "gpt-6-luna",
        "reasoning_effort": "max",
        "allow_inherited_fallback": False,
    }


def test_legacy_lock_directory_has_actionable_error(config):
    lock = config.config.with_name(config.config.name + ".lock")
    lock.mkdir()
    assert "Legacy lock directory" in config.set_model(ok=False)
    assert not config.config.exists()


def test_failed_config_write_restores_prior_agent(config, configure_module, monkeypatch):
    config.set_model(model="old-model")
    before_agent, before_config = config.agent().read_bytes(), config.config.read_bytes()
    previous = configure_module.load_config(config.config)
    desired = {"claude": {"worker": {"model": "new-model"}}}
    original_replace = os.replace

    def fail_config(source, destination):
        if Path(destination) == config.config:
            raise OSError("simulated full disk")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_config)
    with pytest.raises(OSError, match="full disk"):
        configure_module.update_scope(config.project, previous, desired)
    assert config.agent().read_bytes() == before_agent
    assert config.config.read_bytes() == before_config
    assert not config.journal.exists()


@pytest.mark.parametrize("action", ["create", "update", "unconfigure"])
@pytest.mark.parametrize("boundary", ["agent", "config"])
def test_interrupted_writes_are_recovered_and_locks_released(config, action, boundary):
    if action != "create":
        config.set_model(model="old-model")
    before_config = config.config.read_bytes() if config.config.exists() else None
    before_agent = config.agent().read_bytes() if config.agent().exists() else None
    arguments = (
        ("unconfigure",)
        if action == "unconfigure"
        else (
            "set",
            "--harness",
            "claude",
            "--role",
            "worker",
            "--model",
            "new-model",
        )
    )
    config.interrupt(config.agent() if boundary == "agent" else config.config, *arguments)
    assert config.journal.exists()
    resolved = config.cli("show", "--harness", "claude")
    assert resolved["worker"]["model"] == ("sonnet" if action == "create" else "old-model")
    assert (config.config.read_bytes() if config.config.exists() else None) == before_config
    assert (config.agent().read_bytes() if config.agent().exists() else None) == before_agent
    assert not config.journal.exists()
    config.set_model(model="next-model")


def test_interrupted_recovery_can_be_retried(config):
    config.set_model(model="old-model")
    config.interrupt(
        config.agent(), "set", "--harness", "claude", "--role", "worker", "--model", "new-model"
    )
    config.interrupt(config.agent(), "show", "--harness", "claude")
    assert config.journal.exists()
    assert config.cli("show", "--harness", "claude")["worker"]["model"] == "old-model"
    assert not config.journal.exists()


@pytest.mark.parametrize("edited_file", ["agent", "config"])
def test_recovery_preserves_post_crash_edits(config, edited_file):
    config.set_model(model="old-model")
    config.interrupt(
        config.agent(), "set", "--harness", "claude", "--role", "worker", "--model", "new-model"
    )
    target = config.agent() if edited_file == "agent" else config.config
    target.write_text("Post-crash user edit")
    before_agent = config.agent().read_bytes()
    before_config = config.config.read_bytes()
    assert "Preserving edited file during recovery" in config.cli(
        "show", "--harness", "claude", ok=False
    )
    assert config.agent().read_bytes() == before_agent
    assert config.config.read_bytes() == before_config
    assert config.journal.exists()


def test_recovery_rejects_unmanaged_paths(config):
    outside = config.root / "outside"
    outside.write_text("Untouched")
    config.journal.write_text(
        json.dumps(
            {
                "config": {"before": None, "after": None},
                "../outside": {"before": None, "after": outside.read_bytes().hex()},
            }
        )
    )
    assert "Invalid recovery journal" in config.cli("show", "--harness", "claude", ok=False)
    assert outside.read_text() == "Untouched"
