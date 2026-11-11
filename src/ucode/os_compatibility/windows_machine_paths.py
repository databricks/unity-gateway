"""Validate shared Windows runtimes and administrator-controlled publication paths."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os, system_cross_os

_CHECK_ACLS = r"""
$ErrorActionPreference = 'Stop'
$trusted = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
$readers = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
foreach ($path in (ConvertFrom-Json ([Console]::In.ReadToEnd()))) {
    $acl = Get-Acl -LiteralPath $path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $trusted) { throw "Shared path must be owned by Administrators or SYSTEM: $path" }
    $read = 0
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        $sid = $rule.IdentityReference.Value
        $rights = [int]$rule.FileSystemRights
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            if (($rights -band 0xD0156) -and $sid -notin $trusted) {
                throw "Shared path is writable by a non-administrator: $path ($sid)"
            }
            if ($sid -in $readers) { $read = $read -bor $rights }
        } elseif ($sid -in $readers -and ($rights -band 0xA9)) {
            throw "Shared path denies access to device users: $path"
        }
    }
    if (($read -band 0xA9) -ne 0xA9) { throw "Shared path must allow device users read and execute access: $path" }
}
"""


def reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & (
            stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError(f"Refusing linked machine path: {candidate}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError(f"Not a regular machine path: {candidate}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise RuntimeError(f"Refusing hard-linked machine file: {candidate}")


def validate(path: Path, *, required: bool = False) -> None:
    root = system_cross_os.windows_program_files_dir()
    if not path.is_absolute() or not path.is_relative_to(root):
        raise RuntimeError(f"Install shared Windows runtimes under {root}: {path}")
    reject_links(path)
    if required and not path.exists():
        raise RuntimeError(f"Shared machine path is missing: {path}")
    paths = [p for p in (path, *path.parents) if p.is_relative_to(root) and p.exists()]
    powershell = system_cross_os.windows_system32_dir() / "WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess_cross_os.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _CHECK_ACLS],
        input=json.dumps([str(p) for p in paths]),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise PermissionError(f"Unsafe shared Windows path: {path}. {result.stderr.strip()}")


def validate_tree(root: Path) -> None:
    reject_links(root)
    if not root.exists():
        return
    for path in root.rglob("*"):
        reject_links(path)
