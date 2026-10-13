"""Wait for eventually consistent CUJ evidence."""

import time


def poll(fn, *, timeout, interval, done=bool):
    """Call `fn` until `done` accepts its value or `timeout` elapses; return the last value."""
    deadline = time.monotonic() + timeout
    while not done(value := fn()):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return value
