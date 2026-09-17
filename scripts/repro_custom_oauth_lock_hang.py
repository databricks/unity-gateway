#!/usr/bin/env python3
"""Reproduce the old OAuth lock hang or verify the owner-lease fix, offline."""

import argparse
import fcntl
import multiprocessing
import time
from pathlib import Path

from ucode.custom_oauth import CustomOAuthFlowTimeout, _custom_oauth_lock

DEFAULT_LOCK = Path("/tmp/ug-oauth-lock-repro/ug-oauth-8020.lock")


def _raw_owner(lock_path, ready, seconds):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        ready.set()
        time.sleep(seconds + 60)


def _leased_owner(lock_path, ready, seconds):
    try:
        with _custom_oauth_lock(
            lock_path.parent,
            "http://localhost:8020/callback",
            lease_seconds=seconds,
        ):
            ready.set()
            time.sleep(seconds + 60)
    except CustomOAuthFlowTimeout:
        pass


def _waiter(lock_path, acquired):
    with _custom_oauth_lock(
        lock_path.parent,
        "http://localhost:8020/callback",
        lease_seconds=5,
    ):
        acquired.set()


def _stop(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(5)


def run(mode, seconds):
    lock_path = DEFAULT_LOCK
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    acquired = context.Event()
    owner_target = _raw_owner if mode == "broken" else _leased_owner
    owner = context.Process(target=owner_target, args=(lock_path, ready, seconds))
    waiter = context.Process(target=_waiter, args=(lock_path, acquired))
    processes = (owner, waiter)

    try:
        owner.start()
        if not ready.wait(5):
            raise SystemExit("Owner failed to acquire the lock")
        waiter.start()
        waiter.join(seconds + 2)

        if mode == "broken":
            if acquired.is_set():
                raise SystemExit("Unexpectedly acquired the lock")
            print("BROKEN REPRODUCED: the waiter is still blocked behind the hung owner.")
        else:
            if waiter.is_alive() or not acquired.is_set():
                raise SystemExit("FIX FAILED: the waiter did not acquire the released lock")
            print("FIX VERIFIED: the owner lease expired and the waiter acquired the lock.")
    finally:
        _stop(processes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("broken", "fixed"))
    parser.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    run(args.mode, args.seconds)


if __name__ == "__main__":
    main()
