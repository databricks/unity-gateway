"""Shared fixtures for the native Codex component tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def native_binary():
    binary = Path(os.environ.get("UCODE_TEST_CODEX_BINARY", ""))
    expected = os.environ.get("UCODE_TEST_CODEX_VERSION", "")
    assert binary.is_absolute() and binary.is_file() and expected, (
        "Set UCODE_TEST_CODEX_BINARY to an absolute executable and UCODE_TEST_CODEX_VERSION."
    )
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"codex-cli {expected}", result.stdout
    return str(binary)
