"""
Global sliding-window rate limiter for all Sarvam API calls.

All pipeline modules that call the Sarvam API must call throttle()
before each request to stay within the account's RPM limit.

Thread-safety: the window is protected by a lock, but the lock is NEVER
held across a sleep — otherwise every other worker thread would block for
the full wait period and kill parallelism. Threads sleep outside the lock
and then re-check the window on the next iteration.
"""
import threading
import time

from .config import SARVAM_RPM_LIMIT

_lock = threading.Lock()
_timestamps: list[float] = []


def throttle() -> None:
    """Block the calling thread until one more request fits within SARVAM_RPM_LIMIT/60s."""
    while True:
        with _lock:
            now = time.monotonic()
            # Drop timestamps older than 60s
            while _timestamps and now - _timestamps[0] >= 60.0:
                _timestamps.pop(0)
            if len(_timestamps) < SARVAM_RPM_LIMIT:
                _timestamps.append(now)
                return
            # Need to wait until the oldest timestamp falls out of the window.
            sleep_for = 60.0 - (now - _timestamps[0])

        # Sleep OUTSIDE the lock so other threads can still check / record.
        # Add a small jitter floor to avoid a thundering-herd re-check.
        time.sleep(max(sleep_for, 0.01))
