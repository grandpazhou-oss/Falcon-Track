"""A shared, cross-process request lease for public website collection.

The lease remains held until the response is consumed (or the request fails),
then leaves at least one second before any cooperating collector starts again.
It does not cover model APIs, ordinary browsers, or external search tools.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
from filelock import FileLock

MIN_INTERVAL = 1.1


def rate_directory():
    # The same user must share a rate guard even when LEAD_DATA_DIR differs.
    # Override only when *all* cooperating processes use the same directory.
    return Path(os.environ.get('LEAD_PUBLIC_RATE_DIR') or
                (Path(tempfile.gettempdir()) / 'logistics-lead-radar-public-rate')).expanduser().resolve()


class RequestLease:
    def __init__(self, timeout=300):
        self.directory = rate_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = FileLock(str(self.directory / 'public-request.lock'), timeout=timeout)
        self.state = self.directory / 'public-request-last.txt'
        self.acquired = False

    def acquire(self):
        self.lock.acquire()
        self.acquired = True
        try:
            now = time.monotonic()
            try:
                last = float(self.state.read_text(encoding='ascii'))
                # Monotonic clocks restart on reboot; corrupted values fail safe.
                elapsed = now - last if 0 <= last <= now else 0
            except (OSError, ValueError, OverflowError):
                elapsed = 0 if self.state.exists() else MIN_INTERVAL
            if elapsed < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - elapsed)
            # Mark start as well as completion so a crashed process still leaves a guard.
            self.state.write_text(str(time.monotonic()), encoding='ascii')
        except BaseException:
            self.acquired = False
            self.lock.release()
            raise
        return self

    def release(self):
        if not self.acquired:
            return
        try:
            self.state.write_text(str(time.monotonic()), encoding='ascii')
        finally:
            self.acquired = False
            self.lock.release()


@contextmanager
def public_request(timeout=300):
    lease = RequestLease(timeout).acquire()
    try:
        yield
    finally:
        lease.release()
