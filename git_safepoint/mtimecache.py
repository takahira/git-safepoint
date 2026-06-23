#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file-signature cache for incremental snapshotting.

State lives in ``.git/snap/mtime-cache.json`` (inside the git dir, so it never
shows up in ``ls-files`` and never needs a .gitignore tweak). A file is
re-hashed only when its ``[mtime_ns, size, inode, exec_bit, ctime_ns, type]``
signature changes; otherwise the previously written blob SHA is reused.

Writes go to a temp file + ``os.replace`` so a crash never leaves half-written
JSON. The cache is a pure performance optimisation: a missing / corrupt cache
just means a cold (full re-hash) pass, never a wrong snapshot.
"""
from __future__ import annotations

import json
import os
import stat
from typing import Dict, List, Optional

# v2: the signature gained a 4th field (the executable bit) so a chmod-only
# change -- which updates ctime, not mtime -- is no longer skipped by the
# conservative / debounce fast-skip.
# v3: added a 5th field, ctime_ns. ctime advances on every content
# write and cannot be rolled back from user space, so an mtime-restoring tool
# (``tar -x`` / ``rsync --times`` / ``cp -p`` / inode reuse) that re-creates an
# identical [mtime, size, inode, exec] no longer fools the conservative skip into
# reusing a stale blob.
# v4: added a 6th field, the file-type bits S_IFMT. A regular file and
# a symlink that swap at the same path (inode reuse, same mtime/size/exec) would
# otherwise reuse the wrong-typed blob -- now the type change is caught.
# Bumping the version makes load() ignore older caches and cold-rehash once.
CACHE_VERSION = 4

# Number of leading signature fields (everything before the stored blob SHA).
SIG_LEN = 6


class MtimeCache:
    """Path -> [mtime_ns, size, ino, exec_bit, ctime_ns, type, blob_sha] signature map."""

    def __init__(self, path: str):
        self.path = path
        self.entries: Dict[str, List] = {}
        # SHA of the snapshot commit this cache last corresponded to. The
        # conservative/debounce fast-skip uses it to confirm a covering snapshot
        # still exists before skipping a capture -- a prune (possibly from another
        # worktree) can delete that snapshot while the signatures still match,
        # which would otherwise drop the protective capture. None for
        # a cache written before this field existed -> the guard declines to skip.
        self.last_commit: Optional[str] = None
        # [mtime_ns, size] of the real .git/index at the last capture. The
        # conservative/debounce fast-skip only inspects the WORK TREE, so a
        # staged-only change (git add -p) leaves the work-tree
        # signature unchanged and would be skipped, silently dropping the staged
        # variant; we also defeat the skip when the index file changed (an index
        # write always bumps mtime/size). None for an old cache -> defeat skip
        # (safe).
        self.index_sig: Optional[List] = None

    # ---- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "MtimeCache":
        cache = cls(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
                ent = data.get("entries", {})
                if isinstance(ent, dict):
                    # Keep only well-formed entries: a list carrying the SIG_LEN
                    # signature fields PLUS the trailing blob SHA. A version-
                    # matching but malformed entry (non-list, or too short) would
                    # otherwise crash reuse_sha / signature_set mid-capture (slice
                    # / index error) instead of degrading to a cold rehash. A
                    # dropped entry is simply a cache miss.
                    cache.entries = {
                        k: v for k, v in ent.items()
                        if isinstance(v, list) and len(v) > SIG_LEN
                    }
                lc = data.get("last_commit")
                if isinstance(lc, str) and lc:
                    cache.last_commit = lc
                isig = data.get("index_sig")
                if isinstance(isig, list) and len(isig) == 2:
                    cache.index_sig = isig
        except (OSError, ValueError):
            # Corrupt / missing cache -> behave as cold (empty). Correctness is
            # preserved; only the speed-up is lost.
            cache.entries = {}
        return cache

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp.{0}".format(os.getpid())
        payload = {
            "version": CACHE_VERSION,
            "entries": self.entries,
            "last_commit": self.last_commit,
            "index_sig": self.index_sig,
        }
        # 0o600 via os.open: the cache lists every captured path + blob SHA, so
        # keep it owner-only regardless of umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- signatures --------------------------------------------------------

    @staticmethod
    def signature(st: os.stat_result) -> List[int]:
        """Signature [mtime_ns, size, inode, exec_bit, ctime_ns, type].

        - exec_bit: a chmod-only change (bumps ctime, not mtime/size/inode) still
          changes the signature and is captured.
        - ctime_ns: on POSIX (macOS / Linux, the supported platforms) content
          writes always advance ctime, and it cannot be rolled back via
          ``os.utime``, so an mtime-restoring tool that re-creates an
          otherwise-identical signature (``tar -x`` / ``rsync --times`` / ``cp
          -p`` / inode reuse) no longer slips a stale blob past the conservative /
          debounce fast-skip. ``st`` is already taken by the caller,
          so this adds no syscall. (On Windows ``st_ctime`` is creation time and
          would not advance on writes -- it degrades safely to the old behaviour,
          never a false capture, but adds no extra protection there.)
        - type (S_IFMT): a regular-file <-> symlink swap at the same path with a
          reused inode and identical mtime/size/exec would otherwise reuse the
          previous blob under the new mode (corruption on restore); the type bit
          forces a re-hash on any type change.
        """
        exec_bit = 1 if (st.st_mode & stat.S_IXUSR) else 0
        return [st.st_mtime_ns, st.st_size, st.st_ino, exec_bit,
                st.st_ctime_ns, stat.S_IFMT(st.st_mode)]

    def reuse_sha(self, rel: str, sig: List[int]) -> Optional[str]:
        """Return the cached blob SHA when the signature matches, else None."""
        prev = self.entries.get(rel)
        if prev and prev[:SIG_LEN] == sig:
            return prev[SIG_LEN]
        return None

    def record(self, rel: str, sig: List[int], sha: str) -> None:
        self.entries[rel] = list(sig) + [sha]

    def signature_set(self) -> Dict[str, tuple]:
        """Map path -> signature tuple for debounce change detection."""
        return {p: tuple(v[:SIG_LEN]) for p, v in self.entries.items()}
