#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Snapshot ID minting: ``YYYYMMDD-HHMMSS-NNNN-PID``.

Second resolution alone collides on rapid back-to-back snapshots, so we add a
*persistent* per-repo counter (``.git/snap/seq`` on the COMMON git dir, so it is
shared across linked worktrees; read-modify-written under the repo lock) plus the
PID as a final tie-breaker. The counter is durable across processes (the hook and
preexec run as separate forks) and across worktrees, so it stays monotonic even
when two unrelated processes -- or two worktrees of the same repo -- snapshot in
the same second.
"""
from __future__ import annotations

import calendar
import os
import time
from typing import Optional

SEQ_WIDTH = 4
SEQ_MODULO = 10 ** SEQ_WIDTH  # NNNN wraps at 10000, monotonic within that window


def _read_seq(seq_path: str) -> int:
    try:
        with open(seq_path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        return int(raw) if raw else 0
    except (OSError, ValueError):
        return 0


def _write_seq(seq_path: str, value: int) -> None:
    """Atomically persist the counter (temp + os.replace), owner-only."""
    os.makedirs(os.path.dirname(seq_path), exist_ok=True)
    tmp = seq_path + ".tmp.{0}".format(os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(value))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, seq_path)


def next_seq(seq_path: str) -> int:
    """Read-modify-write the persistent counter, returning the new value.

    Must be called under the repo lock. Returns a value in ``[1, SEQ_MODULO)``.
    """
    nxt = (_read_seq(seq_path) + 1)
    # Keep NNNN within 4 digits; the persisted value can be the wrapped one.
    nxt_mod = nxt % SEQ_MODULO
    if nxt_mod == 0:
        nxt_mod = 1
    _write_seq(seq_path, nxt_mod)
    return nxt_mod


def make_id(seq: int, epoch: Optional[float] = None, pid: Optional[int] = None) -> str:
    """Build ``YYYYMMDD-HHMMSS-NNNN-PID`` from a sequence number.

    The timestamp is formatted in UTC (``gmtime``) so that :func:`epoch_from_id`
    round-trips exactly and is TZ-independent. Local time repeats wall-clock
    hours across a DST fold, mapping two distinct epochs to the same string and
    skewing retention by up to an hour.
    """
    if epoch is None:
        epoch = time.time()
    if pid is None:
        pid = os.getpid()
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(epoch))
    return "{0}-{1:0{2}d}-{3}".format(ts, seq, SEQ_WIDTH, pid)


def epoch_from_id(snap_id: str) -> int:
    """Best-effort parse the leading timestamp of an ID back to epoch seconds.

    Parses as UTC (``calendar.timegm``) to match :func:`make_id`. Falls back to
    0 when the prefix is not a valid timestamp.
    """
    try:
        head = "-".join(snap_id.split("-")[:2])  # YYYYMMDD-HHMMSS
        struct = time.strptime(head, "%Y%m%d-%H%M%S")
        return calendar.timegm(struct)
    except (ValueError, IndexError):
        return 0
