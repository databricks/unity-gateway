# Testing the publish-skills action

The action is independent of the Unity Gateway CLI package. From
`actions/publish-skills`, create a Python 3.12 virtual environment and run:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
ruff check .
ruff format --check .
```

The `Publish skills action` workflow runs tests with Python 3.12 and 3.13,
checks runtime dependencies for vulnerabilities, and exercises the composite action
with directory discovery and explicit mappings. Checks run without workspace
credentials on pull requests, main pushes, and merge groups. The existing repository
zizmor workflow checks workflow and action definitions.

End-to-end tests start a loopback HTTP server and run the real CLI and SDK. They
cover creation, recursive and binary files, updates, pruning, unchanged reruns,
error reporting, source SHA, and dry-run modes. They do not prove live authentication.

## Live workspace validation

Use a dedicated nonproduction schema owned by the test identity. From the action directory:

```bash
python tests/live_roundtrip.py --profile YOUR_TEST_PROFILE \
  --schema YOUR_CATALOG.YOUR_TEST_SCHEMA --report /tmp/live-roundtrip.json
```

This creates or updates only `<test-schema>.publisher_roundtrip`. It verifies binary
bytes, updates, pruning, unchanged reruns, and preservation of owner and grants.
It leaves the isolated skill for inspection. Never use a customer or production schema.

For a full source repository:

```bash
python publish.py --source-directory /path/to/repo --skills-directory skills \
  --target-schema YOUR_CATALOG.YOUR_TEST_SCHEMA --profile YOUR_TEST_PROFILE \
  --report /tmp/publish.json --dry-run
```

Remove `--dry-run` to publish, then rerun unchanged and expect zero uploads/removals.
Local profile authentication does not validate a customer's GitHub OIDC federation
or OAuth setup. See [validation](../POC.md) and [authentication](authentication.md).
