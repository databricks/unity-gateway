#!/usr/bin/env python3
"""Reproduce the smart-routing / custom-catalog model-id race.

The race
--------
At launch, smart routing builds ``available_models`` from one of two sources
(`v2.launch_codex` / `codex._launch_smart_routing`)::

    available_models = custom_catalog_models() or _cached_routing_models(state)

  * ``custom_catalog_models()`` -> bare hyphenated slugs from the per-workspace
    MPS catalog (``codex/v1/models``), e.g. ``gpt-5-6-luna``.  This is also the
    router's own arm vocabulary.
  * ``_cached_routing_models()`` -> ``system.ai.``-prefixed discovery ids cached
    in state, e.g. ``system.ai.gpt-5-6-luna``.  This is the fallback when the
    catalog file is absent or didn't load in time.

Both lists normalize to the same router arms (via ``_normalize_route_model``),
so the router picks correctly either way.  The bug is one step later: the
routed model is passed through ``codex_model_id`` to produce the dotted alias
the gateway actually resolves (``gpt-5.6-luna``).  Before the fix,
``codex_model_id`` only translated *prefixed* ids; a bare slug like
``gpt-5-6-luna`` was returned unchanged, which the gateway cannot resolve.

So whether the catalog loaded in time decided which dialect
``available_models`` was in, and only the prefixed dialect got dotted-ized.
Users on the catalog path got the broken ``gpt-5-6-luna``; users on the
fallback path got the working ``gpt-5.6-luna`` alias.

Run this script to see both dialects converge to ``gpt-5.6-luna`` after the
fix.  To see the pre-fix divergence, revert the one-line change in
``codex_routing.codex_model_id`` (``else: bare = tail`` -> ``else: return model``).
"""
from __future__ import annotations

from ucode.smart_routing import codex_routing

ROUTER_ARM = "gpt-5-6-luna"

SCENARIOS = [
    ("catalog present (bare slugs)", ["gpt-5-6-luna", "gpt-5-6-sol"]),
    ("catalog absent / fallback (system.ai. prefixed)", [
        "system.ai.gpt-5-6-luna",
        "system.ai.gpt-5-6-sol",
    ]),
]


def simulate(codex_routing_mod, available_models: list[str], router_arm: str) -> dict:
    """Simulate the full model-translation path for one launch scenario.

    Mirrors the three places ``codex_model_id`` is applied:
      - start_model       : ``codex._launch_smart_routing`` -> ``codex_model_id(models[0])``
      - interposer rewrite: ``codex_interposer._Session``  -> ``codex_model_id(decision.model)``
      - subagent route    : ``codex_routing.route_pre_tool_use`` -> ``model_id_mapper``
    All three reduce to ``codex_model_id`` on the available_models entry the
    router resolved to, so we compute that once.
    """
    cr = codex_routing_mod
    available = {cr._normalize_route_model(m): m for m in available_models}
    resolved = available.get(cr._normalize_route_model(router_arm))
    if resolved is None:
        return {"resolved": None, "start_model": None, "routed_model": None}
    return {
        "resolved": resolved,
        "start_model": cr.codex_model_id(available_models[0]),
        "routed_model": cr.codex_model_id(resolved),
    }


def main() -> int:
    print("=" * 72)
    print("RACE REPRO: codex_model_id translation across available_models dialects")
    print("=" * 72)

    print("\ncodex_model_id behavior:")
    print(f"  codex_model_id('gpt-5-6-luna')          = {codex_routing.codex_model_id('gpt-5-6-luna')!r}")
    print(f"  codex_model_id('system.ai.gpt-5-6-luna') = {codex_routing.codex_model_id('system.ai.gpt-5-6-luna')!r}")

    print("\nScenario -> model sent to the gateway (via codex_model_id):")
    results: dict[str, dict] = {}
    for label, models in SCENARIOS:
        r = simulate(codex_routing, models, ROUTER_ARM)
        results[label] = r
        print(f"  [{label}]")
        print(f"      available_models = {models}")
        print(f"      router arm       = {ROUTER_ARM!r} -> resolved {r['resolved']!r}")
        print(f"      start_model      = {r['start_model']!r}")
        print(f"      routed model     = {r['routed_model']!r}")

    routed_models = {r["routed_model"] for r in results.values()}
    if len(routed_models) == 1:
        model = next(iter(routed_models))
        print(f"\n  => Both dialects converge to {model!r}")
        assert model == "gpt-5.6-luna", f"unexpected model: {model}"
        print("  FIX VERIFIED: catalog-present and catalog-absent paths produce")
        print("  the same gateway-resolvable dotted alias.")
        return 0
    else:
        print("\n  => DIVERGENCE (the race):")
        for label, r in results.items():
            print(f"       {label:55s} -> {r['routed_model']}")
        print("  Users on the catalog path get a model the gateway can't resolve;")
        print("  users on the fallback path get the working dotted alias.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
