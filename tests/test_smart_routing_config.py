"""Exhaustive component coverage for versioned smart-routing configuration."""

from __future__ import annotations

from itertools import product

import pytest

from ucode.constants import (
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTING_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing import config

_EXPECTED_PRESETS = {
    "subagent_only_v0": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_only_v1": {
        ENABLE_SMART_ROUTING_ENV_VAR: "1",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "0",
    },
    "subagent_orch_v0": {
        ENABLE_SMART_ROUTING_ENV_VAR: "0",
        ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1",
    },
}

_LEGACY_VALUES = (None, "", "0", "1")
_SELECTOR_CASES = (
    ("selector-unset", None, "legacy", None),
    ("selector-blank", "", "legacy", None),
    ("selector-whitespace", " \t", "legacy", None),
    ("subagent-only-v0", "subagent_only_v0", "preset", _EXPECTED_PRESETS["subagent_only_v0"]),
    ("subagent-only-v1", "subagent_only_v1", "preset", _EXPECTED_PRESETS["subagent_only_v1"]),
    ("subagent-orch-v0", "subagent_orch_v0", "preset", _EXPECTED_PRESETS["subagent_orch_v0"]),
    (
        "padded-subagent-only-v0",
        " subagent_only_v0 ",
        "preset",
        _EXPECTED_PRESETS["subagent_only_v0"],
    ),
    (
        "padded-subagent-only-v1",
        " subagent_only_v1 ",
        "preset",
        _EXPECTED_PRESETS["subagent_only_v1"],
    ),
    (
        "padded-subagent-orch-v0",
        " subagent_orch_v0 ",
        "preset",
        _EXPECTED_PRESETS["subagent_orch_v0"],
    ),
    ("unsuffixed-subagent-only", "subagent_only", "invalid", None),
    ("unsuffixed-subagent-orch", "subagent_orch", "invalid", None),
    ("unsupported-version", "future_mode", "invalid", None),
)

_GRID = [
    pytest.param(
        v2_value,
        subagent_value,
        orchestrator_value,
        selector,
        selector_mode,
        expected_flags,
        id=(
            f"{selector_id}-v2={v2_value!r}-subagent={subagent_value!r}-"
            f"orchestrator={orchestrator_value!r}"
        ),
    )
    for selector_id, selector, selector_mode, expected_flags in _SELECTOR_CASES
    for v2_value, subagent_value, orchestrator_value in product(_LEGACY_VALUES, repeat=3)
]


@pytest.mark.parametrize(
    (
        "v2_value",
        "subagent_value",
        "orchestrator_value",
        "selector",
        "selector_mode",
        "expected_flags",
    ),
    _GRID,
)
def test_smart_routing_config_cartesian_grid(
    v2_value,
    subagent_value,
    orchestrator_value,
    selector,
    selector_mode,
    expected_flags,
):
    source = {
        environment_key: value
        for environment_key, value in (
            (ENABLE_SMART_ROUTING_ENV_VAR, v2_value),
            (ENABLE_SUBAGENT_ROUTING_ENV_VAR, subagent_value),
            (ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR, orchestrator_value),
        )
        if value is not None
    }
    source["UNRELATED_SETTING"] = "preserved"
    if selector is not None:
        source[SMART_ROUTING_CONFIG_VERSION_ENV_VAR] = selector
    original = source.copy()

    if selector_mode == "invalid":
        with pytest.raises(RuntimeError):
            config.resolve_environment(source)
        assert source == original

        applied = source.copy()
        with pytest.raises(RuntimeError):
            config.apply_config(applied)
        assert applied == original
        return

    expected_legacy = original.copy()
    expected_legacy.pop(SMART_ROUTING_CONFIG_VERSION_ENV_VAR, None)
    expected_resolved = expected_legacy.copy()
    if selector_mode == "preset":
        expected_resolved.update(expected_flags)

    resolved = config.resolve_environment(source)

    assert resolved == expected_resolved
    assert source == original

    applied = source.copy()
    config.apply_config(applied)

    expected_applied = expected_resolved if selector_mode == "preset" else original
    assert applied == expected_applied
