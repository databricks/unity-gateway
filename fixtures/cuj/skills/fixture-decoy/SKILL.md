---
name: fixture-decoy
description: Out-of-scope CUJ skill used to check managed schema boundaries.
---

# Fixture decoy

This skill belongs to `ug_e2e.other_skills`. It should be visible in the catalog
to the CUJ service principal, but never installed as a managed skill by the CUJ3
configuration that points to `ug_e2e.skills`.

When explicitly asked to exercise this decoy, call `decoy_status` with the run ID
and report the returned value.
