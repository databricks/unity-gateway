#!/usr/bin/env python3
"""Reproduce a hung UG custom-OAuth helper blocking another helper.

This is entirely offline: it takes the same file lock introduced by PR #620,
but does not open a browser, read credentials, or contact a workspace.
"""

import argparse
import fcntl
import multiprocessing
import os
import signal
from pathlib import Path

DEFAULT_LOCK = Path.home() / ".config/databricks-sdk-py/oauth/ug-oauth-8020.lock"


def acquire_and_wait(lock_path, acquired, release):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        acquired.set()
        release.wait()
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def acquire_and_report(lock_path, acquired):
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        acquired.set()
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def ensure_lock_is_free(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                f"The lock is already held: {lock_path}\n"
                f"Find its owner with: fuser -v {lock_path}"
            ) from None
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def automatic_repro(lock_path, blocked_seconds):
    ensure_lock_is_free(lock_path)
    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    release_holder = context.Event()
    waiter_acquired = context.Event()

    holder = context.Process(
        target=acquire_and_wait,
        args=(lock_path, holder_acquired, release_holder),
        name="hung-auth-token",
    )
    waiter = context.Process(
        target=acquire_and_report,
        args=(lock_path, waiter_acquired),
        name="second-auth-token",
    )

    try:
        holder.start()
        if not holder_acquired.wait(5):
            raise SystemExit("The simulated hung helper could not acquire the lock")
        print(f"Hung auth helper PID {holder.pid} holds {lock_path}")

        waiter.start()
        if waiter_acquired.wait(blocked_seconds):
            raise SystemExit("Reproduction failed: the second helper unexpectedly acquired the lock")

        print(
            f"REPRODUCED: second auth helper PID {waiter.pid} remained blocked for "
            f"{blocked_seconds:g}s"
        )
        print(f"While blocked, the owner is visible with: fuser -v {lock_path}")

        release_holder.set()
        holder.join(5)
        waiter.join(5)
        if holder.is_alive() or waiter.is_alive() or not waiter_acquired.is_set():
            raise SystemExit("Cleanup failed: a child process did not exit normally")
        print("Released the holder; the second helper acquired the lock and exited.")
    finally:
        release_holder.set()
        for process in (holder, waiter):
            if process.pid is not None:
                process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(5)


def manual_repro(lock_path):
    ensure_lock_is_free(lock_path)
    stopping = multiprocessing.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        print(f"PID {os.getpid()} holds {lock_path}", flush=True)
        print("Start Claude Code or Codex through UG now; press Ctrl-C here to release.", flush=True)
        while not stopping.wait(0.2):
            pass
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    print("Lock released.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--blocked-seconds",
        type=float,
        default=2,
        help="how long the automatic repro must observe the second helper blocked",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="hold the lock until Ctrl-C so a real UG launch can be tested",
    )
    args = parser.parse_args()
    if args.blocked_seconds <= 0:
        parser.error("--blocked-seconds must be positive")
    lock_path = args.lock_path.expanduser().resolve()
    if args.manual:
        manual_repro(lock_path)
    else:
        automatic_repro(lock_path, args.blocked_seconds)


if __name__ == "__main__":
    main()
