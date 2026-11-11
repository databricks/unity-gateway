"""Check shared helper access from a real unprivileged Windows account."""

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

runtime, host, profile = sys.argv[1:]
assert not ctypes.windll.shell32.IsUserAnAdmin(), "Consumer must not be an administrator"
username = ctypes.create_unicode_buffer(256)
length = ctypes.c_ulong(len(username))
assert ctypes.windll.advapi32.GetUserNameW(username, ctypes.byref(length))
assert username.value == "ug-native-consumer", "Wrong consuming Windows account"
consumer_env = dict(os.environ)
for name, folder_id in (("USERPROFILE", 40), ("APPDATA", 26), ("LOCALAPPDATA", 28)):
    folder = ctypes.create_unicode_buffer(260)
    assert ctypes.windll.shell32.SHGetFolderPathW(None, folder_id, None, 0, folder) == 0
    consumer_env[name] = folder.value
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
