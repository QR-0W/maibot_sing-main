"""Cross-process scheduler ownership, separate from the in-service ML lock.

Nonblocking locks keep SDK handlers responsive. The launch helper inherits the
open file description, so a host restart cannot drop launch ownership early.
"""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import stat


class OwnershipBusy(RuntimeError):
    pass


@contextmanager
def exclusive(path: Path):
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Unsafe ownership lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Ownership lock must be a regular file')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OwnershipBusy('Another coordinator or launch helper still owns this job') from exc
        yield fd
    finally:
        # Do NOT LOCK_UN: an inherited helper may still hold the same open file
        # description after the host stops. Last close releases the flock.
        os.close(fd)
