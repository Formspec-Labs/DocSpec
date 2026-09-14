"""Nonblocking process locks; callers own descriptors and path admission."""

from contextlib import contextmanager
import errno
import fcntl

from docspec.errors import StateTransitionError


@contextmanager
def lock_descriptor(descriptor: int, *, shared: bool = False, busy_message: str):
    try:
        fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise StateTransitionError(busy_message) from error
        raise
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
