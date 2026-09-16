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
import time
from pathlib import Path
from queue import Empty

DEFAULT_LOCK = Path.home() / ".config/databricks-sdk-py/oauth/ug-oauth-8020.lock"


def acquire_and_wait(lock_path, acquired, release):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{os.getpid()}\n".encode())
        lock_file.flush()
        acquired.set()
        release.wait()
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def acquire_and_report(lock_path, acquired):
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        acquired.set()
        fcntl.flock(lock_file, fcntl.LOCK_UN)


def hold_with_ug_lease(lock_path, lease_seconds, acquired, result):
    from ucode.custom_oauth import (
        CustomOAuthFlowTimeout,
        _custom_oauth_lock,
    )

    port = int(lock_path.stem.removeprefix("ug-oauth-"))
    try:
        with _custom_oauth_lock(
            lock_path.parent,
            f"http://localhost:{port}/callback",
            timeout_seconds=1,
            lease_seconds=lease_seconds,
        ):
            acquired.set()
            time.sleep(lease_seconds + 60)
    except CustomOAuthFlowTimeout as exc:
        result.put(("expired", str(exc)))


def acquire_after_owner(lock_path, wait_seconds, result):
    from ucode.custom_oauth import _custom_oauth_lock

    prefix = "ug-oauth-"
    if not lock_path.stem.startswith(prefix):
        result.put(("error", f"lock filename must look like {prefix}<port>.lock"))
        return
    port = int(lock_path.stem.removeprefix(prefix))
    with _custom_oauth_lock(
        lock_path.parent,
        f"http://localhost:{port}/callback",
        timeout_seconds=wait_seconds,
        lease_seconds=wait_seconds,
    ):
        result.put(("acquired", "waiter acquired the lock after the owner's lease expired"))


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


def verify_fix(lock_path, lease_seconds):
    ensure_lock_is_free(lock_path)
    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    holder_result = context.Queue()
    waiter_result = context.Queue()
    holder = context.Process(
        target=hold_with_ug_lease,
        args=(lock_path, lease_seconds, holder_acquired, holder_result),
        name="hung-auth-token",
    )
    waiter = context.Process(
        target=acquire_after_owner,
        args=(lock_path, lease_seconds + 5, waiter_result),
        name="bounded-auth-token",
    )
    try:
        holder.start()
        if not holder_acquired.wait(5):
            raise SystemExit("The simulated hung helper could not acquire the lock")
        waiter.start()
        holder.join(lease_seconds + 5)
        waiter.join(lease_seconds + 10)
        if holder.is_alive() or waiter.is_alive():
            raise SystemExit("FIX FAILED: a helper remained blocked beyond the owner's lease")
        try:
            holder_outcome, holder_detail = holder_result.get(timeout=1)
            waiter_outcome, waiter_detail = waiter_result.get(timeout=1)
        except Empty:
            raise SystemExit("FIX FAILED: a helper exited without reporting an outcome") from None
        if holder_outcome != "expired" or waiter_outcome != "acquired":
            raise SystemExit(f"FIX FAILED: {holder_detail}; {waiter_detail}")
        print(f"OWNER EVICTED: {holder_detail}")
        print(f"FIX VERIFIED: {waiter_detail}.")
        print("Only the lock owner can enter OAuth at any time, so browser flows remain serialized.")
    finally:
        for process in (holder, waiter):
            if process.pid is not None:
                process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(5)
        for result in (holder_result, waiter_result):
            result.close()
            result.join_thread()


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
    parser.add_argument(
        "--verify-fix",
        metavar="SECONDS",
        type=float,
        help="use UG's real lock helper and verify an owner is evicted after SECONDS",
    )
    args = parser.parse_args()
    if args.blocked_seconds <= 0:
        parser.error("--blocked-seconds must be positive")
    lock_path = args.lock_path.expanduser().resolve()
    if args.verify_fix is not None and args.verify_fix <= 0:
        parser.error("--verify-fix must be positive")
    if args.manual and args.verify_fix is not None:
        parser.error("--manual and --verify-fix cannot be used together")
    if args.manual:
        manual_repro(lock_path)
    elif args.verify_fix is not None:
        verify_fix(lock_path, args.verify_fix)
    else:
        automatic_repro(lock_path, args.blocked_seconds)


if __name__ == "__main__":
    main()
