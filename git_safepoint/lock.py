#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repo-level advisory lock via ``fcntl.flock`` on .git/snap/lock.

Snapshot and prune bodies run under this exclusive, non-blocking lock so the
mtime cache, the persistent sequence counter and ref creation cannot race
between the hook / preexec / manual entry points.
"""
from __future__ import annotations

import errno
import os
import time
from typing import Optional

try:
    import fcntl  # POSIX only; macOS / Linux have it.

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore
    _HAVE_FCNTL = False


class LockBusy(Exception):
    """Raised when the repo lock is held by another process."""


class RepoLock:
    """Non-blocking exclusive lock as a context manager.

    Usage::

        with RepoLock(lock_path):
            ...  # critical section

    Raises :class:`LockBusy` if another process holds the lock. When fcntl is
    unavailable the lock degrades to a no-op (single-process correctness only).
    """

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if not _HAVE_FCNTL:
            return
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockBusy(self.lock_path) from exc
            # flock is unsupported on this mount (lockd-less NFS, an
            # flock-rejecting overlay/network FS, or an exhausted lock table):
            # ENOLCK / EINVAL / ENOTSUP / EOPNOTSUPP. DEGRADE to a no-op, exactly
            # like the ``_HAVE_FCNTL is False`` branch -- re-raising here made the
            # protective hook capture fail-open and SILENTLY skip the snapshot
            # (the safety net disabling itself), and crashed manual snapshot /
            # restore / prune with an uncaught traceback. Single-process
            # correctness only, the same tradeoff the README documents for NFS;
            # the collision-free ref mint still prevents ref-store corruption.
            if exc.errno in (
                errno.ENOLCK, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP
            ):
                return  # leave self._fd None -> release() is a no-op
            raise
        self._fd = fd

    def acquire_blocking(self, timeout: float = 10.0, poll: float = 0.05) -> bool:
        """Try to acquire for up to ``timeout`` seconds; return whether we got it.

        Used by restore, which wants to serialise against a concurrent capture
        (so a capture cannot snapshot a half-restored tree) but must never hard
        fail just because a brief background capture holds the lock. The caller
        proceeds without the lock when this returns False. With fcntl absent the
        lock is a no-op, so we report success.
        """
        if not _HAVE_FCNTL:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self.acquire()
                return True
            except LockBusy:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "RepoLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
