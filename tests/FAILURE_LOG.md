# Test failure log

Every fixed test failure gets one line here, newest last. After two weeks of entries, files whose
failures were all change detectors are deleted and their patterns banned in `AGENTS.md`. Tests for
authorization, privacy, migrations or concurrency are never deleted, whatever their record.

- **caught regression**: the test failed because product behavior broke, and the fix was in `src/`.
- **change detector**: the test failed on a correct change because it pinned an implementation
  detail (exact wording, call order, private structure), and the fix was in the test.

Collection started 2026-11-21.

| date | test | kind | PR | what changed |
|---|---|---|---|---|
| 2026-11-21 | `tests/test_smart_router_feedback.py::test_native_rejects_unbound_sessions` | change detector | #1823 | parametrize id built from `uuid4()` broke pytest-xdist collection; now a fixed id |
| 2026-11-21 | `tests/test_tui_request_recorder.py::test_claude_protocol_projects_only_safe_response_metadata` | change detector | #1823 | gzip bytes embedded the current time in the test id; now `mtime=0` |
| 2026-11-21 | `tests/test_configure_managed_writes.py` revert-output assertions | change detector | #1823 | asserted where an 80-column wrap fell inside a temp path; now whitespace-insensitive |
| 2026-11-21 | `tests/test_cli.py::TestAutoCleanupLifecycle::test_revert_retry_keeps_target_after_source_mapping_is_restored_away` | change detector | #1823 | same wrap dependence |
| 2026-11-21 | `tests/test_desktop_auth_cli.py::test_desktop_auth_use_pat_returns_the_profile_pat` | change detector | #1823 | depended on the runner's real `databricks` CLI answering within 20 s |
