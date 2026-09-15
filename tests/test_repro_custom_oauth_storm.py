"""Process-level checks using the offline OAuth repro's simulated browser."""

from scripts.repro_custom_oauth_storm import run_helpers


def test_concurrent_helpers_share_one_login(tmp_path):
    result = run_helpers(tmp_path, helpers=4, login_seconds=0.5, timeout=15)
    assert result.browsers == 1
    assert result.succeeded == 4
    assert result.failed == result.timed_out == 0


def test_killed_helper_releases_lock_for_next_login(tmp_path):
    interrupted = run_helpers(tmp_path, helpers=1, login_seconds=10, timeout=0.5)
    assert interrupted.browsers == 1
    assert interrupted.timed_out == 1

    recovered = run_helpers(tmp_path, helpers=1, login_seconds=0.01, timeout=15)
    assert recovered.browsers == recovered.succeeded == 1
    assert recovered.failed == recovered.timed_out == 0

    cached = run_helpers(tmp_path, helpers=2, login_seconds=10, timeout=5)
    assert cached.browsers == 0
    assert cached.succeeded == 2
    assert cached.failed == cached.timed_out == 0
