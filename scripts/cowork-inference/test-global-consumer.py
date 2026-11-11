"""Check shared helper access from a real unprivileged Windows account."""

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

runtime, host, profile = sys.argv[1:]
assert not ctypes.windll.shell32.IsUserAnAdmin(), "Consumer must not be an administrator"
username = ctypes.create_unicode_buffer(256)
length = ctypes.c_ulong(len(username))
assert ctypes.windll.advapi32.GetUserNameW(username, ctypes.byref(length))
assert username.value == "ug-native-consumer", "Wrong consuming Windows account"
ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
ctypes.windll.advapi32.OpenProcessToken.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
)
token = wintypes.HANDLE()
assert ctypes.windll.advapi32.OpenProcessToken(
    ctypes.windll.kernel32.GetCurrentProcess(), 8, ctypes.byref(token)
)
block = ctypes.c_void_p()
ctypes.windll.userenv.CreateEnvironmentBlock.argtypes = (
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.BOOL,
)
ctypes.windll.userenv.DestroyEnvironmentBlock.argtypes = (ctypes.c_void_p,)
account_env = {}
try:
    assert ctypes.windll.userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False)
    address = block.value
    assert address is not None
    while entry := ctypes.wstring_at(address):
        if not entry.startswith("="):
            name, value = entry.split("=", 1)
            account_env[name.upper()] = value
        address += (len(entry) + 1) * ctypes.sizeof(ctypes.c_wchar)
finally:
    if block.value:
        ctypes.windll.userenv.DestroyEnvironmentBlock(block)
    ctypes.windll.kernel32.CloseHandle(token)
consumer_env = dict(os.environ)
for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
    assert account_env.get(name), f"Windows did not initialize {name} for the consumer"
    consumer_env[name] = account_env[name]
consumer_env.pop("HOME", None)
consumer_env["CLAUDE_HELPER_CONTEXT"] = "silent"
assert Path(consumer_env["USERPROFILE"]).name == username.value
assert consumer_env.get("DATABRICKS_BEARER"), (
    "Provisioning credential isolation needs an inherited credential"
)
assert Path(runtime).read_bytes(), "Shared runtime is unreadable"
try:
    with Path(runtime).open("ab"):
        pass
except PermissionError:
    pass
else:
    raise AssertionError("Consumer can modify the shared helper")

help_result = subprocess.run(
    [sys.executable, "-I", runtime, "--help"],
    stdin=subprocess.DEVNULL,
    env=consumer_env,
    capture_output=True,
    text=True,
    timeout=60,
)
assert help_result.returncode == 0, "Consumer cannot run the installed shared helper"
auth_result = subprocess.run(
    [sys.executable, "-I", runtime, "claude-desktop-auth", "--host", host, "--profile", profile],
    stdin=subprocess.DEVNULL,
    env=consumer_env,
    capture_output=True,
    text=True,
    timeout=45,
)
assert auth_result.returncode != 0, (
    "Fresh consumer unexpectedly inherited provisioning authentication"
)
assert not auth_result.stdout.strip(), "Failed authentication wrote a credential to stdout"
assert "Databricks authentication expired" in auth_result.stderr, (
    "Expected missing user authentication, not a helper startup/configuration failure"
)
print(
    json.dumps(
        {
            "account": username.value,
            "administrator": False,
            "readable": True,
            "writable": False,
            "helperExecutable": True,
            "provisioningCredentialRejected": True,
        }
    )
)
