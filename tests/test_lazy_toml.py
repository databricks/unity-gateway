"""Fresh-process checks for deferred TOML writers and their existing exports."""

import subprocess
import sys


def _run(code: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_and_readonly_app_settings_leave_toml_writer_unloaded(tmp_path):
    settings = tmp_path / "config.toml"
    settings.write_text('model = "test-model"\n', encoding="utf-8")
    _run(
        """
import sys
from pathlib import Path
import ucode.cli
from ucode.agents import codex
assert 'tomlkit' not in sys.modules
codex._legacy_config_path = lambda: Path(sys.argv[1])
assert codex._read_app_config_values() == {'model': 'test-model'}
assert 'tomlkit' not in sys.modules
""",
        str(settings),
    )


def test_named_and_wildcard_toml_exports_keep_their_identity():
    _run(
        """
import sys
from ucode import codex_config
from ucode.agents import codex
assert {'tomlkit', 'Item'} <= set(dir(codex_config))
assert {'tomlkit', 'ParseError'} <= set(dir(codex))
assert 'tomlkit' not in sys.modules
from ucode.codex_config import Item, tomlkit
from ucode.agents.codex import ParseError
assert Item is tomlkit.items.Item
assert ParseError is tomlkit.exceptions.ParseError
for module, expected in [(codex_config, {'tomlkit': tomlkit, 'Item': Item}),
                         (codex, {'tomlkit': tomlkit, 'ParseError': ParseError})]:
    namespace = {}
    exec(f'from {module.__name__} import *', namespace)
    for name, value in expected.items():
        assert namespace[name] is value
"""
    )


def test_config_serialization_loads_writer_and_preserves_output():
    _run(
        """
import sys
from ucode.codex_config import codex_config_args
assert 'tomlkit' not in sys.modules
assert codex_config_args({'model': 'test-model'}) == ['--config', 'model="test-model"']
assert 'tomlkit' in sys.modules
"""
    )
