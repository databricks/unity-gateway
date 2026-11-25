"""Collect native Windows UIA fixture diagnostics after a test failure."""

import ctypes
import json
import os
import shutil
import subprocess
from ctypes import wintypes


def _process_facts(kernel, security, process_id):
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.IsWow64Process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.WORD),
    ]
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    process = kernel.OpenProcess(0x1000, False, process_id)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    try:
        machine, native = wintypes.WORD(), wintypes.WORD()
        if not kernel.IsWow64Process2(process, ctypes.byref(machine), ctypes.byref(native)):
            raise ctypes.WinError(ctypes.get_last_error())
        effective = machine.value or native.value
        bits = {0x014C: 32, 0x8664: 64, 0xAA64: 64}.get(effective)
        if not security.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 25, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        count = security.GetSidSubAuthorityCount(sid)[0]
        integrity = security.GetSidSubAuthority(sid, count - 1)[0]
        level = {
            0x0000: "untrusted",
            0x1000: "low",
            0x2000: "medium",
            0x2100: "medium-plus",
            0x3000: "high",
            0x4000: "system",
        }.get(integrity, "unknown")
        return {
            "bitness": bits,
            "machine": effective,
            "integrity_rid": integrity,
            "integrity_level": level,
        }
    finally:
        if token.value:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def _child_windows(user, process_id):
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.RealGetWindowClassW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.UINT]
    parents, children = [], set()

    @callback_type
    def parent(hwnd, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id:
            parents.append(hwnd)
        return True

    @callback_type
    def child(hwnd, _):
        children.add(hwnd)
        return True

    if not user.EnumWindows(parent, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    for hwnd in parents:
        user.EnumChildWindows(hwnd, child, 0)
    result = []
    for hwnd in sorted(children):
        name, real = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        if not user.GetClassNameW(hwnd, name, len(name)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not user.RealGetWindowClassW(hwnd, real, len(real)):
            raise ctypes.WinError(ctypes.get_last_error())
        result.append(
            {"handle": int(hwnd), "class_name": name.value, "real_class_name": real.value}
        )
    return result


def _uia_properties(children):
    if not children:
        return {}
    powershell = shutil.which("powershell.exe") or "powershell.exe"
    script = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$rows = foreach ($value in ($env:UIA_DIAGNOSTIC_HANDLES -split ',')) {
    if (-not $value) { continue }
    $element = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new([long]$value))
    [pscustomobject]@{handle=[long]$value; framework_id=$element.Current.FrameworkId; uia_class_name=$element.Current.ClassName}
}
ConvertTo-Json -InputObject @($rows) -Compress
"""
    env = {**os.environ, "UIA_DIAGNOSTIC_HANDLES": ",".join(str(row["handle"]) for row in children)}
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )
    return {row["handle"]: row for row in json.loads(result.stdout)}


def capture_native_environment(fixture_pid, helper_pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    result = {}
    for label, pid in (("fixture", fixture_pid), ("helper", helper_pid)):
        try:
            result[label] = _process_facts(kernel, security, pid)
        except OSError as error:
            result[label] = {"error": str(error)}
    try:
        children = _child_windows(user, fixture_pid)
    except OSError as error:
        result["children_error"] = str(error)
        return result
    try:
        uia = _uia_properties(children)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        result["uia_error"] = str(error)
    else:
        for row in children:
            row.update(
                {key: value for key, value in uia.get(row["handle"], {}).items() if key != "handle"}
            )
    result["children"] = children
    return result
