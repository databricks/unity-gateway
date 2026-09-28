"""File-sourced managed config loader for --config-file invocations.

Provides :func:`load_file_managed_config`, which reads a full published CodingAgentConfig from disk
once per invocation, validates it, and returns the normalized manifest.  The result is identical in
shape to what :func:`ucode.managed_config.refresh_managed_config` produces from a live fetch.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ucode.managed_config import _gate_config, normalize_managed_config


# A hand-authored config must be unambiguous, so parse stricter than stdlib json: reject duplicate
# object keys (which would silently last-win) and non-finite numbers, whether written as a constant
# (NaN/Infinity) or reached by overflow (e.g. 1e999), none of which a real managed config contains.
def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("Invalid --config-file: duplicate JSON object key.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> float:
    raise RuntimeError("Invalid --config-file: non-finite JSON numbers are not allowed.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError("Invalid --config-file: non-finite JSON numbers are not allowed.")
    return number


def load_file_managed_config(path: str) -> dict:
    """Read a --config-file once, validate it as a full CodingAgentConfig, and return the
    NORMALIZED manifest (same shape normalize_managed_config produces). Raise RuntimeError with a
    clear, actionable message on any invalid input."""
    try:
        contents = Path(path).expanduser().read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Invalid --config-file: expected valid UTF-8 JSON.") from exc
    except OSError as exc:
        raise RuntimeError("Cannot read --config-file; supply a readable JSON file.") from exc
    try:
        raw = json.loads(
            contents,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except ValueError as exc:
        # ValueError covers JSONDecodeError plus the ValueError json raises on an out-of-range int
        # literal, so a malformed file never escapes as a raw traceback.
        raise RuntimeError("Invalid --config-file: expected valid JSON.") from exc

    if not isinstance(raw, dict):
        raise RuntimeError("Invalid --config-file: expected a JSON object.")

    # Apply the same spec_version forward-compat gate the live fetch uses, so a file declaring a
    # version this build can't act on is refused here instead of applied when the fetch would refuse.
    spec_reason = _gate_config(raw).reason
    if spec_reason is not None:
        raise RuntimeError(f"Invalid --config-file: {spec_reason}")

    # normalize_managed_config (via CodingAgentConfig.from_wire) IS the wire-schema validator.
    # It is the same path the API fetch uses; metadata keys are silently dropped, unknown agents
    # are skipped, and all structural issues surface as exceptions here before any caller writes.
    try:
        return normalize_managed_config(raw)
    except Exception as exc:
        raise RuntimeError(
            f"Invalid --config-file: the configuration is not a valid CodingAgentConfig ({exc})."
        ) from exc
