"""Workspace identity canonicalization without changing CLI input normalization."""

import pytest

from ucode.ui import normalize_workspace_url
from ucode.workspace_urls import canonical_workspace_origin


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        ("workspace.example", "https://workspace.example"),
        (" workspace.example/ ", "https://workspace.example"),
        ("https://WORKSPACE.example:443///", "https://workspace.example"),
        ("HTTPS://workspace.example", "https://workspace.example"),
        ("http://WORKSPACE.example:80/", "http://workspace.example"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("https://workspace.example:8443", "https://workspace.example:8443"),
        ("http://workspace.example:443", "http://workspace.example:443"),
        ("https://workspace.example:80", "https://workspace.example:80"),
        ("https://[::1]:443/", "https://[::1]"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
    ],
)
def test_canonical_workspace_origin(workspace: str, expected: str) -> None:
    assert canonical_workspace_origin(workspace) == expected


@pytest.mark.parametrize(
    "workspace",
    [
        "",
        "   ",
        "https://",
        "ftp://workspace.example",
        "https://user@workspace.example",
        "https://user:password@workspace.example",
        "https://workspace.example/api",
        "https://workspace.example?query=value",
        "https://workspace.example#fragment",
        "https://workspace.example:invalid",
        "https://workspace.example:65536",
    ],
)
def test_canonical_workspace_origin_rejects_non_origin_urls(workspace: str) -> None:
    with pytest.raises(ValueError):
        canonical_workspace_origin(workspace)


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        (" https://WORKSPACE.example:443/// ", "https://WORKSPACE.example:443"),
        ("http://WORKSPACE.example:80/", "http://WORKSPACE.example:80"),
        ("https://workspace.example/path", "https://workspace.example/path"),
    ],
)
def test_cli_workspace_normalization_keeps_existing_behavior(workspace: str, expected: str) -> None:
    assert normalize_workspace_url(workspace) == expected
