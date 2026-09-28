"""Catalog derive worker stand-ins that fail the pool: one dies mid-task, one ignores shutdown and never returns.

Spawned workers import these by module path, so they live here rather than in
a test module.
"""

import os
import signal
import time


def die_mid_task(args):
    """Kill this worker the way the OOM killer would, before the partition's summary is sent."""
    os.kill(os.getpid(), signal.SIGKILL)


def ignore_shutdown(args):
    """Ignore the pool's termination signal and never finish, like the worker that hung two runs on 2026-09-27."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(60)
