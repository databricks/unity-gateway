"""Unit checks for the Windows job object the integration helpers start VS Code in."""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

# Starts a child that outlives it, then exits at once, like a VS Code process orphaned on quit.
# The child gets no inherited pipes, or the test's communicate() would wait for it too.
PARENT = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
    "print('started', flush=True)"
)


def test_the_job_keeps_a_child_whose_parent_exited_until_it_is_closed():
    from tests.integration.utils.harness import WindowsJob

    job = WindowsJob()
    try:
        parent = job.popen([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
        out, _ = parent.communicate(timeout=30)
        assert out.strip() == "started"
        assert parent.returncode == 0
        assert job.active_processes() >= 1
        assert not job.wait_until_empty(0.5)
    finally:
        job.close()
    assert job.handle is None


def test_the_job_empties_once_its_processes_exit():
    from tests.integration.utils.harness import WindowsJob

    job = WindowsJob()
    try:
        job.popen([sys.executable, "-c", "pass"]).wait(timeout=30)
        assert job.wait_until_empty(10)
        assert job.active_processes() == 0
    finally:
        job.close()
