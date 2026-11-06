---
feature: upgrade
title: Upgrade ug (ug upgrade)
platform: cli
paths: [src/ucode/cli.py]
status: 'unverified: deliberately not run; it reinstalls the tool from GitHub. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug upgrade

Reinstall ug from `git+https://github.com/databricks/unity-gateway` with uv (`cli.py:5502`).

## Reach

`ug upgrade` (no options). It is under "Manage" in `ug --help`.

## Drive

1. [unverified] `ug upgrade`. Do not run it in a shared environment.

## Expect (from code, not observed)

- Runs `uv tool install --reinstall <dist> @ git+https://github.com/databricks/unity-gateway`, where `<dist>` is the installed distribution (`unity-gateway` or legacy `ucode`).
- On the legacy `ucode` to `unity-gateway` cutover it uninstalls `ucode` and installs fresh (`cli.py:5520-5545`).
- A non-zero uv exit prints an error with the failure detail and exits 1.

## Gotchas

- It replaces the installed tool in the user's real `~/.local`; an isolated HOME does not help because uv's tool dir comes from the environment.
- The `ug` on PATH here reports `0.1.0+6fb87ac` while the checkout is `0.2.1+83.gf83f1c65`. Saved auth commands use the PATH `ug`.
