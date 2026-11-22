"""Which certificate authorities ``ug``'s own HTTPS requests trust.

``os`` (the default) verifies against the operating system's trust store (Keychain on macOS, the
certificate store on Windows, the system CA bundle on Linux), which is where an enterprise
TLS-inspection proxy's root certificate is installed and what curl, browsers, and ``ug mcp-proxy``
(through httpx2) already read. ``python`` keeps Python's own certificate chain instead: the CA
bundle the interpreter's OpenSSL reads plus ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` for urllib,
certifi or ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` for httpx, and certifi or ``REQUESTS_CA_BUNDLE``
for ``requests`` (the Databricks SDK, the metrics exporter). Behind an inspection proxy that chain
lacks the proxy's root, so requests fail with
``CERTIFICATE_VERIFY_FAILED`` while curl succeeds.

The choice comes from ``UG_TLS_TRUST_STORE``, then ``tls_trust_store`` in ``~/.ucode/state.json``
(saved by ``ug configure --tls-trust-store``), then the default. It is applied once per process,
before any request, and covers every client in it (urllib, httpx, requests, the Databricks SDK).
Agents and the Databricks CLI are separate programs with their own trust settings.

Kept free of heavy imports: every ``ug`` process, including the detached helpers, runs
:func:`apply` at startup.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TRUST_STORE_ENV_VAR = "UG_TLS_TRUST_STORE"
TRUST_STORE_STATE_KEY = "tls_trust_store"
TRUST_STORE_PYTHON = "python"
TRUST_STORE_OS = "os"
TRUST_STORES = (TRUST_STORE_PYTHON, TRUST_STORE_OS)
DEFAULT_TRUST_STORE = TRUST_STORE_OS

_STATE_PATH = Path.home() / ".ucode" / "state.json"
_applied: str | None = None


def _warn(message: str) -> None:
    print(f"ug: warning: {message}", file=sys.stderr)


def _normalize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in TRUST_STORES else None


def _read_state() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_trust_store() -> str | None:
    """The trust store saved in ``state.json``, or None when unset or not a known value."""
    return _normalize(_read_state().get(TRUST_STORE_STATE_KEY))


def resolve_trust_store() -> str:
    """``UG_TLS_TRUST_STORE``, else the saved choice, else the default.

    An unknown value warns and falls through to the next source, so a typo never silently
    changes which certificates are trusted."""
    raw = os.environ.get(TRUST_STORE_ENV_VAR, "")
    if raw.strip():
        if (from_env := _normalize(raw)) is not None:
            return from_env
        _warn(f"ignoring {TRUST_STORE_ENV_VAR}={raw!r}; use one of: {', '.join(TRUST_STORES)}.")
    saved = _read_state().get(TRUST_STORE_STATE_KEY)
    if saved is not None:
        if (from_state := _normalize(saved)) is not None:
            return from_state
        _warn(
            f"ignoring {TRUST_STORE_STATE_KEY}={saved!r} in {_STATE_PATH}; "
            f"use one of: {', '.join(TRUST_STORES)}."
        )
    return DEFAULT_TRUST_STORE


def apply(trust_store: str | None = None) -> str:
    """Make this process verify HTTPS against ``trust_store`` (resolved when omitted).

    Safe to call repeatedly and to switch: ``os`` injects ``truststore`` into ``ssl`` so every
    client built afterwards uses the OS store, ``python`` removes it again. A failure to load
    the OS store warns and leaves Python's chain in place rather than blocking the command.
    Returns the trust store now in effect."""
    global _applied
    if trust_store is None:
        if _applied is not None:
            # Already resolved for this process; a second entry point must not warn again.
            return _applied
        trust_store = resolve_trust_store()
    wanted = trust_store
    if wanted == _applied:
        return wanted
    if wanted == TRUST_STORE_OS:
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception as exc:  # noqa: BLE001 - never block a command on trust-store setup
            _warn(
                f"could not use the OS certificate store ({exc}); using Python's certificate chain."
            )
            _applied = TRUST_STORE_PYTHON
            return _applied
    elif _applied == TRUST_STORE_OS:
        import truststore

        truststore.extract_from_ssl()
    _applied = wanted
    return wanted


def effective_trust_store() -> str:
    """The trust store this process verifies against (resolved now if :func:`apply` hasn't run)."""
    return _applied if _applied is not None else resolve_trust_store()


def is_certificate_failure(error: object) -> bool:
    """Whether ``error`` (an exception, or a URLError's ``reason``) is a failed verification."""
    import ssl

    return isinstance(error, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(
        error
    )


def certificate_failure_hint() -> str:
    """What to do about a ``CERTIFICATE_VERIFY_FAILED``, for the trust store in effect."""
    bundle = "point SSL_CERT_FILE and REQUESTS_CA_BUNDLE at a CA bundle that includes it"
    if effective_trust_store() == TRUST_STORE_OS:
        return (
            "ug verifies against the operating system's trust store. Behind a TLS-inspection "
            "proxy, add the proxy's root certificate to that store, or set "
            f"{TRUST_STORE_ENV_VAR}={TRUST_STORE_PYTHON} and {bundle}."
        )
    return (
        "ug is verifying against Python's certificate chain. Behind a TLS-inspection proxy, "
        f"set {TRUST_STORE_ENV_VAR}={TRUST_STORE_OS} (or run `ug configure --tls-trust-store "
        f"{TRUST_STORE_OS}`) to use the operating system's trust store, or {bundle}."
    )


def uv_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """``environment`` (default: this process's) for a ``uv`` child that installs packages.

    With the OS trust store in effect, uv reads it too, so ``ug upgrade`` works behind the same
    inspection proxy. ``UV_SYSTEM_CERTS`` is the current switch and ``UV_NATIVE_TLS`` the one
    older uv releases read; a value the user already set is kept."""
    env = dict(os.environ if environment is None else environment)
    if effective_trust_store() == TRUST_STORE_OS:
        env.setdefault("UV_SYSTEM_CERTS", "1")
        env.setdefault("UV_NATIVE_TLS", "1")
    return env


def save_trust_store(trust_store: str) -> None:
    """Save ``trust_store`` as ``tls_trust_store`` in ``state.json`` for later ``ug`` runs."""
    from ucode.config_io import atomic_write_json, is_dry_run
    from ucode.state import STATE_PATH, load_full_state

    if trust_store not in TRUST_STORES:
        raise ValueError(f"Unknown TLS trust store: {trust_store}")
    full = load_full_state()
    if is_dry_run() or full.get(TRUST_STORE_STATE_KEY) == trust_store:
        return
    full[TRUST_STORE_STATE_KEY] = trust_store
    atomic_write_json(STATE_PATH, full)
