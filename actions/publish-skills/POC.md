# Validation results

The migrated publisher was validated on 2026-11-03 against an isolated nonproduction
Unity Catalog schema using local Databricks profile authentication. No notebook,
job, cluster, or Unity Gateway CLI installation was needed.

Source: [databricks/databricks-agent-skills](https://github.com/databricks/databricks-agent-skills),
commit `a2d7a4e4ed2d72a66d690203475e0e808be48fc1`.

| Check | Result |
| --- | --- |
| Initial directory publish | 34 skills created; 354 files uploaded |
| Unchanged full-repository rerun | 34 unchanged; zero uploads or removals |
| Custom mapped target | Passed |
| Binary and nested file bytes | Passed |
| Update and explicit stale-file pruning | Passed |
| Removal without opt-in pruning | Rejected as expected |
| Owner and grant preservation across updates | Passed |
| Unit and real SDK-over-HTTP CLI tests | 38 passed after review fixes |
| Hosted composite-action smoke tests | Both directory and mapping dry runs passed |
| Hosted publisher tests | Python 3.12 and 3.13 passed, including dependency audit |

The local staging test validates publisher API behavior with profile authentication.
The hosted smoke tests require no workspace credentials. Customer GitHub OIDC
federation and OAuth M2M configuration are separate prerequisites; these runs do
not establish live hosted authentication for those setups.

In-place updates are not atomic. This is not a large-scale performance benchmark.
Hosting in this repository does not establish an additional product support SLA.
See [testing](docs/testing.md) to reproduce the tests in your own isolated schema.

Review validation also ran the live roundtrip with `python -O`; explicit checks verified remote bytes, pruning, ownership, and grants without relying on Python assertions.
