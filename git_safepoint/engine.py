#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The single snapshot engine: capture, list, diff, restore, prune.

Built on git plumbing (``hash-object -w`` + private temp index +
``write-tree`` / ``commit-tree`` + shadow ref ``refs/snapshots/*``). The user's
index and HEAD are never touched. Capture is incremental via the mtime cache,
debounced, locked, and uses a process-spanning persistent counter for
collision-free IDs.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

from . import gitutil, ids, secret
from .lock import LockBusy, RepoLock
from .mtimecache import MtimeCache

SNAP_REF_PREFIX = gitutil.SNAP_REF_PREFIX

COMMIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "snapshot",
    "GIT_AUTHOR_EMAIL": "snapshot@local",
    "GIT_COMMITTER_NAME": "snapshot",
    "GIT_COMMITTER_EMAIL": "snapshot@local",
}

MAX_REF_RETRIES = 64
LABEL_MAX = 60

# A snapshot stamped more than this far in the future was almost certainly
# captured during a forward clock jump; prune demotes such refs so they cannot
# masquerade as "newest" and evict genuine snapshots.
_CLOCK_SKEW_MARGIN = 24 * 3600  # 1 day

# Paths per ``git hash-object`` invocation when batching. With ARG_MAX ~= 1 MiB
# (macOS/Linux), 500 paths stays well under even with long paths, while turning
# N per-file ``git`` forks into ceil(N/500) -- the dominant cold-capture cost.
HASH_BATCH_MAX = 500


class SnapshotResult:
    """Outcome of a capture attempt."""

    def __init__(
        self,
        status: int,
        snap_id: Optional[str] = None,
        ref: Optional[str] = None,
        commit: Optional[str] = None,
        files: int = 0,
        reused: int = 0,
        skipped: int = 0,
        elapsed_ms: float = 0.0,
        reason: str = "",
    ):
        self.status = status
        self.id = snap_id
        self.ref = ref
        self.commit = commit
        self.files = files
        self.reused = reused
        self.skipped = skipped
        self.elapsed_ms = elapsed_ms
        self.reason = reason


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


# How long the protective capture path (hook / preexec / default manual) waits
# for the repo lock before giving up. The snapshot the instant before a
# destructive command matters more than a few ms of latency, but we must not
# block the destructive command indefinitely either.
_CAPTURE_LOCK_TIMEOUT = 3.0

# How long a (user-initiated) restore waits for the repo lock before proceeding
# WITHOUT it and warning. A module constant (not a literal) so tests can shrink it
# to exercise the lock-degradation path without blocking for the full interval.
_RESTORE_LOCK_TIMEOUT = 10.0


def mode_from_st(st: os.stat_result) -> str:
    """Git tree mode string from an already-taken lstat: symlink / exec-aware."""
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    if st.st_mode & stat.S_IXUSR:
        return "100755"
    return "100644"


def _gitlink_sha(repo: str, abs_path: str, staged_sha: str) -> str:
    """Commit a submodule gitlink should pin in the snapshot.

    Prefer the submodule's *live* checked-out HEAD -- what is actually there the
    instant before a destructive command -- but only when ``abs_path`` is
    genuinely the submodule's own git worktree. An uninitialised / deinit'd
    submodule is an empty directory, where ``git -C <empty> rev-parse HEAD``
    walks UP and wrongly returns the SUPERPROJECT's HEAD; there we
    fall back to the staged index pin, which is always correct.
    """
    if os.path.exists(os.path.join(abs_path, ".git")):
        head = gitutil.run_git(abs_path, ["rev-parse", "HEAD"], check=False)
        if head.returncode == 0:
            sha = head.stdout.decode("ascii", "ignore").strip()
            if sha:
                return sha
    return staged_sha


def hash_many_blobs(repo: str, abs_paths: List[str]) -> Dict[str, str]:
    """Hash many regular files with one ``git`` process per chunk.

    Returns ``{abs_path: blob_sha}``. The dominant cold-capture cost is forking
    ``git hash-object`` once per file (~10-12 ms/file); batching collapses that
    to ceil(N / :data:`HASH_BATCH_MAX`) forks.

    Robustness, preserved from the per-file path:

    - **Absolute paths only** (the caller passes ``repo/rel``). They never start
      with ``-`` and carry no separator, so odd names (spaces / newlines /
      non-ASCII) pass through ``argv`` intact -- the regression matrix's hard
      cases. (``git hash-object --stdin-paths`` is *not* used: it splits on
      newlines and would corrupt newline-bearing names.)
    - **Symlinks must NOT be passed here** -- ``git hash-object <path>`` follows
      the link and hashes the target's bytes. The caller hashes links one by one
      via :func:`hash_one_blob` (``os.readlink`` + ``--stdin``).
    - **One bad file never aborts the snapshot.** A chunk that exits non-zero OR
      times out (a path that blocks ``git hash-object`` -- e.g. a FIFO opened for
      read, a stuck mount) is re-hashed file-by-file to isolate the offender;
      unhashable / hanging paths are simply omitted from the result and the caller
      skips them. Without catching ``TimeoutExpired`` here it escaped and aborted
      the entire protective snapshot at the worst moment.
    """
    result: Dict[str, str] = {}
    for i in range(0, len(abs_paths), HASH_BATCH_MAX):
        chunk = abs_paths[i:i + HASH_BATCH_MAX]
        try:
            proc = gitutil.run_git(
                repo, ["hash-object", "-w", "--no-filters"] + chunk, check=False
            )
        except subprocess.TimeoutExpired:
            # One file in this chunk blocked the whole `git hash-object` past
            # GIT_TIMEOUT. Don't let it abort the snapshot -- fall through to the
            # per-file retry so the offender is isolated and the rest captured.
            proc = None
        if proc is not None and proc.returncode == 0:
            shas = proc.stdout.decode("utf-8").rstrip("\n").split("\n")
            if len(shas) == len(chunk):
                result.update(zip(chunk, shas))
                continue
        # A path in this chunk failed (missing / unreadable / raced away / timed
        # out). Re-hash one at a time so the rest of the chunk still gets captured
        # and a single hanging path is dropped, not the whole snapshot.
        for path in chunk:
            try:
                result[path] = hash_one_blob(repo, path)
            except (subprocess.CalledProcessError, OSError,
                    subprocess.TimeoutExpired):
                pass  # caller sees the gap and skips this path
    return result


def hash_one_blob(repo: str, abs_path: str) -> str:
    """Write one file (or symlink) to the object DB, returning its blob SHA.

    A symlink stores its *target string* as the blob (mode 120000). Running
    ``git hash-object -w <symlink>`` would follow the link and hash the target
    file's bytes (restoring a broken link, crashing on a dangling link), so we
    read the target with ``os.readlink`` and pipe it via ``--stdin``.
    """
    st = os.lstat(abs_path)
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(abs_path).encode("utf-8", "surrogateescape")
        return (
            gitutil.run_git(
                repo, ["hash-object", "-w", "--stdin"], input_bytes=target
            )
            .stdout.decode("utf-8")
            .rstrip("\n")
        )
    return gitutil.git_out(repo, ["hash-object", "-w", "--no-filters", abs_path])


def _signature_changed(
    repo: str, cache: MtimeCache, files: List[str]
) -> bool:
    """Debounce change detection via stat signatures only (no blob SHA).

    Returns True when the current work tree's signature set differs from the
    cache's (added / removed / changed file), False when nothing changed.
    """
    prev = cache.signature_set()
    cur: Dict[str, tuple] = {}
    for rel in files:
        abs_path = os.path.join(repo, rel)
        try:
            st = os.lstat(abs_path)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            # Directory entries (untracked embedded git repos, submodule
            # gitlinks) are never written to the cache by capture, so including
            # them here would make cur != prev on EVERY call -- permanently
            # defeating the conservative fast-skip and forcing a full re-hash.
            # Skip them to match what is actually cached.
            # A submodule-only HEAD change therefore stays best-effort in
            # conservative mode: this helper reports "no change" and the capture
            # is skipped BEFORE any tree is built, so tree-SHA dedup does NOT
            # guard this path (it only runs once a tree exists). The
            # destructive-moment force_rehash path does not use this helper, so a
            # destructive command still captures the live pin. Documented in
            # README "Known limitations".
            continue
        cur[rel] = tuple(MtimeCache.signature(st))
    return cur != prev


def _recorded_snapshot_present(repo: str, cache: MtimeCache) -> bool:
    """True when the snapshot the cache last recorded still exists as a ref.

    Guards the conservative/debounce fast-skip: after a prune --
    especially one run from ANOTHER worktree sharing ``refs/snapshots`` -- deletes
    that snapshot, the cache's signatures still match the work tree, so the skip
    would wrongly assume a protective snapshot already covers the content and drop
    the capture at the very moment it matters. We confirm the recorded commit is
    still a live snapshot ref before honouring the skip; a cache that predates this
    field (``last_commit`` is None) declines to skip and takes a fresh capture.
    """
    last = cache.last_commit
    if not last:
        return False
    proc = gitutil.run_git(
        repo,
        ["for-each-ref", "--format=%(objectname)", SNAP_REF_PREFIX],
        check=False,
    )
    if proc.returncode != 0:
        return False
    return last in proc.stdout.decode("ascii", "replace").split()


def _index_sig(snap: str) -> Optional[List[int]]:
    """``[mtime_ns, size]`` of the real ``.git/index`` for this work tree, or None.

    ``snap`` is the per-worktree ``<gitdir>/snap`` dir, so the index lives at
    ``<gitdir>/index`` -- ``dirname(snap)/index`` -- for both the main and linked
    worktrees. One ``lstat``, no git fork (unlike ``git_path``), so it is cheap
    enough for the conservative hot path.
    """
    index_path = os.path.join(os.path.dirname(snap), "index")
    try:
        st = os.lstat(index_path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _index_changed(snap: str, cache: MtimeCache) -> bool:
    """True when the real index changed since the cache was written (or can't be
    confirmed). Defeats the conservative/debounce fast-skip so a staged-only
    change -- ``git add -p`` leaving the work tree byte-identical --
    still takes a full capture, where ``_staged_variant_tree`` records the staged
    parent instead of it being silently dropped by the work-tree-only skip.
    """
    cur = _index_sig(snap)
    if cur is None:
        # No ``.git/index`` (the common untracked-only repo): there is no staged
        # state to protect, so the index is not a reason to defeat the fast-skip.
        return False
    prev = getattr(cache, "index_sig", None)
    if not prev:
        return True  # index exists but the cache predates this field -> fresh capture
    return list(prev) != list(cur)


def capture(
    repo: str,
    label: Optional[str] = None,
    via: str = "manual",
    force: bool = False,
    debounce: float = 5.0,
    force_rehash: bool = False,
    no_wait: bool = False,
    include_ignored: Optional[List[str]] = None,
    conservative: bool = False,
) -> SnapshotResult:
    """Capture the work tree. Returns a :class:`SnapshotResult`.

    status: 0 captured, 1 nothing to do / debounced / skip, 2 non-repo.

    - ``via in {"hook", "preexec"}`` implies force_rehash (trust no cache the
      instant before a destructive command) and, because force_rehash distrusts
      the signature, bypasses the debounce too.  Exception: ``conservative=True``
      suppresses force_rehash and instead uses mtime signatures as a fast first
      skip -- suitable for firing on every Bash call, not just destructive ones.
    - ``conservative=True``: skip when mtime+size+inode signatures are unchanged.
      The skip path is O(N) stat lookups plus two cheap git forks -- one
      ``ls-files --deleted`` (deletion-awareness) and one ``for-each-ref`` (the
      guard confirming the recorded snapshot still exists) -- not the full per-file
      re-hash. Changed files are re-hashed and tree-SHA dedup applies as the final
      guard.  Intended for hooks that fire on every Bash call, not just
      destructive commands.
    - ``force`` or ``force_rehash`` ignores debounce.
    - ``no_wait`` (manual): on lock contention, skip with status 1 instead of
      raising.
    - ``include_ignored`` (opt-in): patterns of ``.gitignore``'d paths to also
      capture (e.g. ``["output/"]``). Secret patterns are ALWAYS excluded even
      when matched here (two-stage design; see :mod:`git_safepoint.secret`).
    """
    if not gitutil.is_work_tree(repo):
        return SnapshotResult(2, reason="not a git work tree")

    if via in ("hook", "preexec") and not conservative:
        # The destructive moment (PreToolUse hook OR zsh preexec): trust no cache
        # -- force-rehash so a same-size/same-mtime content swap is still
        # captured, and bypass the signature debounce below (force-rehash exists
        # precisely to distrust the signature). Without this the snapshot the
        # instant before a destructive command could be silently skipped.
        force_rehash = True

    snap = gitutil.snap_dir(repo)            # per-worktree: mtime cache, temp index
    shared = gitutil.shared_snap_dir(repo)   # shared across worktrees: lock, seq
    lock_path = os.path.join(shared, "lock")
    cache_path = os.path.join(snap, "mtime-cache.json")
    seq_path = os.path.join(shared, "seq")

    lock = RepoLock(lock_path)
    if no_wait:
        # Manual ``--no-wait``: don't wait on a concurrent op, just skip.
        try:
            lock.acquire()
        except LockBusy:
            return SnapshotResult(1, reason="another snapshot in progress")
    else:
        # Protective path (hook / preexec / default manual): wait briefly rather
        # than skip. The previous "ride along with the in-flight snapshot"
        # comment was wrong -- the lock holder may be a restore / prune / a stale
        # capture that does NOT cover the current work tree, so silently skipping
        # could lose disk-only unsaved work the instant before a destructive
        # command. If we still cannot get the lock, say so loudly
        # instead of dropping the protective snapshot in silence.
        if not lock.acquire_blocking(timeout=_CAPTURE_LOCK_TIMEOUT):
            sys.stderr.write(
                "  warning: snapshot lock busy for {0:.0f}s; protective "
                "snapshot NOT taken (another git-safepoint op holds the "
                "lock)\n".format(_CAPTURE_LOCK_TIMEOUT)
            )
            return SnapshotResult(
                1, reason="lock busy (protective snapshot skipped)"
            )

    try:
        return _capture_locked(
            repo,
            snap,
            cache_path,
            seq_path,
            label=label,
            via=via,
            force=force,
            debounce=debounce,
            force_rehash=force_rehash,
            include_ignored=include_ignored,
            conservative=conservative,
        )
    finally:
        lock.release()


def _capture_locked(
    repo: str,
    snap: str,
    cache_path: str,
    seq_path: str,
    label: Optional[str],
    via: str,
    force: bool,
    debounce: float,
    force_rehash: bool,
    include_ignored: Optional[List[str]] = None,
    conservative: bool = False,
) -> SnapshotResult:
    t0 = time.time()
    files, dropped_secrets = gitutil.list_worktree_files(
        repo, include_ignored=include_ignored
    )
    if dropped_secrets:
        sys.stderr.write(
            "  note: excluded {0} secret file(s) from snapshot (never "
            "captured): {1}\n".format(
                len(dropped_secrets), ", ".join(dropped_secrets[:5])
            )
        )
    if not files:
        return SnapshotResult(
            1, reason="nothing to snapshot (no tracked+untracked files)"
        )

    cache = MtimeCache.load(cache_path)

    # Deletion-awareness for the fast-skip gates below. A tracked file deleted
    # from the work tree but still staged is listed by ``--cached`` (so it is in
    # ``files``) yet cannot be lstat'd, so ``_signature_changed`` cannot see it
    # when it has no cache entry (cur and prev both omit it -> "no change").
    # Skipping then silently drops a file pending staged-blob injection from the
    # protective snapshot -- the exact moment it matters -- and a later
    # ``git reset --hard`` via an allowlist-missed command makes the staged blob
    # unreachable, so the only copy is lost. Compute the deleted-cached set
    # once here (reused by the injection phase below -> no second fork) and intersect
    # with ``files`` so ONLY a deletion that would actually be injected (non-
    # secret) defeats the skip; a deleted secret stays excluded and would anyway
    # collapse at the tree-SHA dedup.
    deleted_set = set(gitutil.list_deleted_cached(repo))
    deleted_pending = bool(deleted_set & set(files))

    # Conservative mode: mtime signatures as a fast first skip.
    # Fires on every Bash call; only re-hashes when at least one signature changed.
    # Tree-SHA dedup below handles the remaining "changed signature but same bytes"
    # false-positive edge case. The fast-skip also requires that the snapshot this
    # cache recorded STILL EXISTS: a prune -- possibly run from another
    # worktree sharing refs/snapshots -- can delete it while the signatures still
    # match, and skipping then would assume a protective snapshot that is gone.
    if conservative and not force and cache.entries:
        if (not _signature_changed(repo, cache, files) and not deleted_pending
                and not _index_changed(snap, cache)
                and _recorded_snapshot_present(repo, cache)):
            return SnapshotResult(
                1,
                reason="no change (conservative mtime check)",
                elapsed_ms=(time.time() - t0) * 1000,
            )

    # Debounce: skip when the work-tree signature is unchanged since the last
    # snapshot. Despite the name this is a CONTENT check, not a time window --
    # ``debounce > 0`` merely enables it, ``debounce == 0`` disables it.
    # force_rehash (hook/preexec/--force-rehash) bypasses it: the signature is
    # exactly what those callers must not trust (a same-size/same-mtime content
    # swap has an unchanged signature but different bytes). Same
    # snapshot-still-exists guard as the conservative path.
    if not force and not force_rehash and debounce > 0 and cache.entries:
        if (not _signature_changed(repo, cache, files) and not deleted_pending
                and not _index_changed(snap, cache)
                and _recorded_snapshot_present(repo, cache)):
            return SnapshotResult(
                1,
                reason="debounced (no change since last snapshot)",
                elapsed_ms=(time.time() - t0) * 1000,
            )

    tmp_index = os.path.join(snap, "index.capture.{0}".format(os.getpid()))
    if os.path.exists(tmp_index):
        os.unlink(tmp_index)
    index_env = {"GIT_INDEX_FILE": tmp_index}

    new_cache = MtimeCache(cache_path)
    count = 0
    reused = 0
    skipped = 0

    # A tracked file the user just deleted is still listed by ``--cached``
    # but cannot be lstat'd. Without help it falls out of the snapshot ("captured
    # but empty"), defeating the safety net at the exact moment it matters. We
    # inject its staged ``(mode, blob)`` straight into the private index -- the
    # blob is already in the object DB, so this preserves the pre-deletion
    # content with zero re-hash and no dependence on capture timing.
    # ``deleted_set`` was computed once above so the fast-skip gates stay
    # deletion-aware; we reuse it here rather than re-forking ``ls-files``.
    # Staged ``(mode, sha)`` feeds both tracked-deleted injection and submodule
    # gitlink detection. Loaded at most once and only when actually needed
    # (a deleted tracked file or a gitlink turns up), so the common case -- no
    # deletions, no submodules -- pays for no extra ``ls-files`` fork.
    _stage: Dict[str, Tuple[str, str]] = {}
    _stage_loaded = [False]

    def stage_info() -> Dict[str, Tuple[str, str]]:
        if not _stage_loaded[0]:
            _stage.update(gitutil.cached_stage_info(repo))
            _stage_loaded[0] = True
        return _stage

    deleted_inject: List[Tuple[str, str, str]] = []   # (mode, sha, rel)
    gitlink_inject: List[Tuple[str, str, str]] = []   # (160000, commit, rel)

    try:
        # Phase 1: stat + cache lookup. Decide per file whether to reuse a
        # cached blob SHA or hash it, splitting the to-hash set into batchable
        # regular files vs symlinks (which must be hashed one at a time so git
        # does not follow the link).
        entries: List[list] = []  # [rel, abs_path, mode, sig, sha_or_None]
        to_batch: List[str] = []  # abs paths of regular files needing a hash
        to_link: List[str] = []   # abs paths of symlinks needing a hash
        for rel in files:
            abs_path = os.path.join(repo, rel)
            try:
                st = os.lstat(abs_path)
            except OSError:
                # Gone from the work tree. If it is a tracked file deleted but
                # still staged, preserve its cached blob; otherwise it
                # genuinely raced away and is skipped.
                if rel in deleted_set:
                    info = stage_info().get(rel)
                    if info is not None:
                        deleted_inject.append((info[0], info[1], rel))
                continue
            if stat.S_ISDIR(st.st_mode):
                # A listed path that lstat's as a directory is a submodule
                # gitlink ONLY when the index actually stages it as one (mode
                # 160000). It cannot be hashed as a blob (``hash-object -w <dir>``
                # fails -> previously dropped silently as "unhashable");
                # record its pinned commit so the pin survives and is
                # recoverable. An untracked *embedded* git repo is also listed
                # (as ``name/``) but is NOT a submodule -- recording a pin for it
                # would be wrong and ``update-index`` drops the trailing-slash
                # path anyway, so skip it explicitly.
                info = stage_info().get(rel)
                if info is not None and info[0] == "160000":
                    gitlink_inject.append(
                        ("160000", _gitlink_sha(repo, abs_path, info[1]), rel)
                    )
                else:
                    sys.stderr.write(
                        "  warning: skipped {0} (directory / embedded repo, not "
                        "a tracked submodule)\n".format(rel)
                    )
                    skipped += 1
                continue
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                # FIFO / socket / block / char device. ``git hash-object`` would
                # BLOCK forever on a FIFO opened for read (then hit GIT_TIMEOUT),
                # and such nodes are never meaningful snapshot content. Skip them
                # like an embedded repo so one odd node never stalls or aborts the
                # protective snapshot -- the primary guard for the FIFO case;
                # the TimeoutExpired isolation in hash_many_blobs backstops other
                # hang sources (slow mounts, files exceeding GIT_TIMEOUT).
                sys.stderr.write(
                    "  warning: skipped {0} (not a regular file or symlink: "
                    "FIFO/socket/device)\n".format(rel)
                )
                skipped += 1
                continue
            sig = MtimeCache.signature(st)
            mode = mode_from_st(st)
            sha: Optional[str] = None
            if not force_rehash:
                sha = cache.reuse_sha(rel, sig)
            if sha is not None:
                reused += 1
            elif mode == "120000":
                to_link.append(abs_path)
            else:
                to_batch.append(abs_path)
            entries.append([rel, abs_path, mode, sig, sha])

        # Phase 1.5: verify every REUSED blob still exists in the object DB. A
        # cached SHA whose object was pruned / GC'd (e.g. all snapshot refs
        # deleted then ``git gc``, or an aggressive external gc) is accepted by
        # ``update-index --index-info`` but makes the later ``write-tree`` fail
        # hard (exit 128), which aborts the capture BEFORE the cache is rewritten
        # -- so the poisoned entry persists and every subsequent protective
        # snapshot silently dies. Demote a missing reused blob to a real re-hash
        # so capture self-heals, preserving the documented guarantee that the
        # cache is a pure optimisation (worst case: a cold re-hash).
        if not force_rehash:
            reused_shas = {e[4] for e in entries if e[4] is not None}
            missing = gitutil.missing_objects(repo, reused_shas)
            if missing:
                for e in entries:
                    if e[4] is not None and e[4] in missing:
                        e[4] = None  # demote: treat as a cache miss
                        if e[2] == "120000":
                            to_link.append(e[1])
                        else:
                            to_batch.append(e[1])
                        reused -= 1

        # Phase 2: hash. Regular files in batches (one git fork per chunk),
        # symlinks individually via readlink + --stdin.
        hashed = hash_many_blobs(repo, to_batch)
        for abs_path in to_link:
            try:
                hashed[abs_path] = hash_one_blob(repo, abs_path)
            except (subprocess.CalledProcessError, OSError,
                    subprocess.TimeoutExpired):
                pass  # left unhashed (incl. a hanging readlink) -> skipped phase 3

        # Phase 3: feed the private index in the original file order via
        # ``update-index -z --index-info`` on stdin (NUL records
        # ``<mode> SP <sha> TAB <path>``). Unlike a single ``--cacheinfo`` argv,
        # stdin has no ARG_MAX ceiling, so very large trees (10k+ files) build
        # in one process. A file we could not hash (unreadable / odd / raced
        # away) is skipped, never aborting the whole snapshot.
        index_records = bytearray()
        for rel, abs_path, mode, sig, sha in entries:
            freshly_hashed = sha is None  # phase 1 already filled reused blobs
            if sha is None:
                sha = hashed.get(abs_path)
            if sha is None:
                sys.stderr.write("  warning: skipped {0} (unhashable)\n".format(rel))
                skipped += 1
                continue
            index_records += "{0} {1}\t".format(mode, sha).encode("ascii")
            index_records += rel.encode("utf-8", "surrogateescape")
            index_records += b"\x00"
            if freshly_hashed:
                # Re-stat after hashing and only cache this file if its signature
                # was STABLE across the whole capture (phase-1 stat == post-hash
                # stat): then sig and sha provably agree. If it changed mid-capture
                # (or raced away), do NOT cache it -- a stale-but-signature-
                # consistent entry would make a future capture reuse the old blob
                # and silently miss the change. Skipping it makes the next capture
                # cold-rehash and self-heal.
                try:
                    stable = MtimeCache.signature(os.lstat(abs_path)) == sig
                except OSError:
                    stable = False
                if stable:
                    new_cache.record(rel, sig, sha)
            else:
                new_cache.record(rel, sig, sha)
            count += 1

        # Inject staged blobs for tracked-deleted files. No mtime-cache
        # entry (they have no live signature); next capture re-derives them.
        for dmode, dsha, rel in deleted_inject:
            index_records += "{0} {1}\t".format(dmode, dsha).encode("ascii")
            index_records += rel.encode("utf-8", "surrogateescape")
            index_records += b"\x00"
            count += 1
        if deleted_inject:
            sys.stderr.write(
                "  note: preserved {0} deleted tracked file(s) from the "
                "index\n".format(len(deleted_inject))
            )

        # Inject submodule gitlinks (mode 160000). The pinned commit object
        # need not exist in the superproject's object DB -- a tree may reference
        # a gitlink it does not contain (that is how ``git add <submodule>``
        # works), so ``write-tree`` accepts it as-is.
        for gmode, gsha, rel in gitlink_inject:
            index_records += "{0} {1}\t".format(gmode, gsha).encode("ascii")
            index_records += rel.encode("utf-8", "surrogateescape")
            index_records += b"\x00"
            count += 1
        if gitlink_inject:
            sys.stderr.write(
                "  note: recorded {0} submodule pin(s) in the "
                "snapshot\n".format(len(gitlink_inject))
            )

        if count == 0:
            return SnapshotResult(
                1, skipped=skipped, reason="nothing captured (all skipped)"
            )

        gitutil.run_git(
            repo,
            ["update-index", "-z", "--index-info"],
            extra_env=index_env,
            input_bytes=bytes(index_records),
        )
        tree_sha = gitutil.git_out(repo, ["write-tree"], extra_env=index_env)

        # Also protect the STAGED index when ``git add -p`` (or stage-then-
        # edit-more) leaves it differing from BOTH the work tree AND HEAD -- that
        # version is reachable from neither the snapshot's work-tree tree nor any
        # commit, so a later ``reset --hard`` destroys the only copy. When present,
        # carry it as the snapshot commit's parent so the staged blobs stay
        # reachable and restorable via ``restore --staged``. None (the common
        # case: only untracked files, or only unstaged edits) means no parent.
        staged_variant_tree = _staged_variant_tree(repo, snap)

        # Content-level dedup: if the rebuilt tree is byte-identical to the most
        # recent snapshot -- AND the staged-variant state matches too -- don't
        # mint a duplicate ref. This is what lets the destructive-moment paths
        # (hook/preexec/--force-rehash) safely bypass the *signature* debounce --
        # they re-hash to catch a same-size/same-mtime content swap,
        # yet a genuinely unchanged re-fire collapses here instead of spamming
        # identical snapshots. A CHANGED staged state still mints a snapshot.
        # ``--force`` opts out.
        if not force:
            latest = _latest_snapshot_info(repo)
            if (latest is not None and latest[1] == tree_sha
                    and latest[2] == staged_variant_tree):
                # Record the covering snapshot's commit so the fast-skip guard
                # still sees a live snapshot next time, and the index signature so
                # the staged-aware skip starts from the current index.
                new_cache.last_commit = latest[0]
                new_cache.index_sig = _index_sig(snap)
                try:
                    new_cache.save()
                except OSError:
                    pass
                reason = "no change since last snapshot (identical tree)"
                if label:
                    # A supplied --label is silently dropped on a dedup'd unchanged
                    # tree; tell the user how to force-record it.
                    reason += (
                        "; label not recorded -- re-run with --force to bookmark "
                        "the unchanged tree"
                    )
                return SnapshotResult(
                    1,
                    reused=reused,
                    skipped=skipped,
                    reason=reason,
                    elapsed_ms=(time.time() - t0) * 1000,
                )

        parents: List[str] = []
        if staged_variant_tree is not None:
            # Commit the staged tree so it is reachable (gc-safe) as the snapshot
            # commit's parent. Its blobs are already in the object DB (git add
            # wrote them), so this is a single commit-tree, no re-hash.
            staged_commit = gitutil.git_out(
                repo,
                ["commit-tree", staged_variant_tree, "-m", "staged index"],
                extra_env=COMMIT_IDENTITY_ENV,
            )
            parents.append(staged_commit)

        snap_id, ref, commit = _commit_and_ref(
            repo, seq_path, tree_sha, label, via, parents=parents
        )
        # Record the new snapshot's commit (fast-skip guard) and the current index
        # signature (staged-aware skip) for the next fast-skip decision.
        new_cache.last_commit = commit
        new_cache.index_sig = _index_sig(snap)

        # Cache is a pure optimisation; failure to persist it is non-fatal.
        try:
            new_cache.save()
        except OSError as exc:
            sys.stderr.write(
                "  warning: could not write mtime cache ({0})\n".format(exc)
            )

        return SnapshotResult(
            0,
            snap_id=snap_id,
            ref=ref,
            commit=commit,
            files=count,
            reused=reused,
            skipped=skipped,
            elapsed_ms=(time.time() - t0) * 1000,
        )
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)


def _latest_snapshot_info(
    repo: str,
) -> "Optional[Tuple[str, str, Optional[str]]]":
    """``(commit, worktree_tree, staged_parent_tree|None)`` of the newest snapshot.

    - ``worktree_tree`` dedups an identical re-capture so the destructive-moment
      paths can bypass the signature debounce without spamming byte-identical
      snapshots.
    - ``staged_parent_tree`` (the first parent's tree, present only when the index
      differed from the work tree at capture) lets the dedup also
      notice a CHANGED staged state and still mint a snapshot.
    - ``commit`` is recorded in the mtime cache for the fast-skip guard.

    Returns None when there are no snapshots.
    """
    rows = list_refs(repo)
    if not rows:
        return None
    _id, ref = rows[0]
    # commit + its tree in one fork.
    proc = gitutil.run_git(
        repo, ["rev-parse", ref, "{0}^{{tree}}".format(ref)], check=False
    )
    if proc.returncode != 0:
        return None
    parts = proc.stdout.decode("ascii").split()
    if len(parts) < 2:
        return None
    commit, wt_tree = parts[0], parts[1]
    # First-parent tree, if the commit has a parent (the staged variant).
    pproc = gitutil.run_git(
        repo,
        ["rev-parse", "--verify", "--quiet", "{0}^1^{{tree}}".format(ref)],
        check=False,
    )
    staged_tree = (
        pproc.stdout.decode("ascii").strip() if pproc.returncode == 0 else ""
    )
    return commit, wt_tree, (staged_tree or None)


def _staged_variant_tree(repo: str, snap: str) -> Optional[str]:
    """Tree SHA of the staged index WHEN it would be lost on ``reset --hard``.

    The protected case is the ``git add -p`` / stage-then-edit shape:
    the index holds a version that differs from BOTH the work tree AND HEAD, so it
    is reachable from neither the snapshot's work-tree tree nor any commit, and a
    ``reset --hard`` destroys the only copy. Returns that index's tree SHA, or
    None when the index matches the work tree (worktree tree already covers it) or
    matches HEAD (recoverable from HEAD) -- so an ordinary capture with only
    untracked files or only unstaged edits gets NO redundant staged parent.

    Every git call runs against a COPY of the index, so the real ``.git/index`` is
    never touched (the "index and HEAD are never touched" guarantee). Returns None
    on any failure, e.g. an unmerged index where write-tree exits non-zero. The
    referenced blobs are already in the object DB (``git add`` wrote them), so
    write-tree mints only the tree object.
    """
    real_index = gitutil.git_path(repo, "index")
    if not os.path.exists(real_index):
        return None  # fresh repo with no index yet
    tmp_index = os.path.join(snap, "index.staged.{0}".format(os.getpid()))
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        shutil.copyfile(real_index, tmp_index)
        # index vs work tree (tracked files only; untracked are ignored, so they
        # never spuriously trigger a staged parent). rc 1 == they differ.
        unstaged = gitutil.run_git(
            repo, ["diff", "--quiet"], extra_env=env, check=False
        )
        if unstaged.returncode != 1:
            return None  # index == work tree (or error): nothing unique to save
        # index vs HEAD. rc 1 == there is actually something staged vs the commit.
        staged = gitutil.run_git(
            repo, ["diff", "--cached", "--quiet"], extra_env=env, check=False
        )
        if staged.returncode != 1:
            return None  # index == HEAD: recoverable from HEAD, no need to save
        proc = gitutil.run_git(repo, ["write-tree"], extra_env=env, check=False)
    except OSError:
        return None
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)
    if proc.returncode != 0:
        return None
    sha = proc.stdout.decode("ascii", "ignore").strip()
    return sha or None


def _make_message(snap_id: str, label: Optional[str], via: str) -> str:
    msg = "snapshot {0}".format(snap_id)
    if label:
        flat = label.replace("\n", " ")
        # Signal truncation with an ellipsis so a clipped label is recognisable as
        # clipped. Total stays <= LABEL_MAX so snapshot_meta's
        # ``(...)`` parsing and the commit subject stay bounded.
        trimmed = (flat[:LABEL_MAX - 1] + "…") if len(flat) > LABEL_MAX else flat
        msg += " ({0})".format(trimmed)
    msg += " via={0}".format(via)
    return msg


def _commit_and_ref(
    repo: str,
    seq_path: str,
    tree_sha: str,
    label: Optional[str],
    via: str,
    parents: Optional[List[str]] = None,
) -> Tuple[str, str, str]:
    """Mint an ID, commit the tree, and atomically claim the shadow ref.

    Atomic claim: ``update-ref <ref> <commit> ""`` (old value empty = create
    only). On collision (a ref already exists) we advance the counter and retry.

    ``parents`` are extra parent commits for the snapshot commit; the staged-index
    variant is carried as a parent so its blobs stay reachable.
    """
    parent_args: List[str] = []
    for parent in (parents or []):
        parent_args += ["-p", parent]
    last_exc: Optional[Exception] = None
    for _ in range(MAX_REF_RETRIES):
        seq = ids.next_seq(seq_path)
        snap_id = ids.make_id(seq)
        ref = SNAP_REF_PREFIX + snap_id
        msg = _make_message(snap_id, label, via)
        commit = gitutil.git_out(
            repo,
            ["commit-tree", tree_sha] + parent_args + ["-m", msg],
            extra_env=COMMIT_IDENTITY_ENV,
        )
        # Old-value "" means: only succeed if the ref does not yet exist.
        proc = gitutil.run_git(
            repo,
            ["update-ref", "--create-reflog", ref, commit, ""],
            check=False,
        )
        if proc.returncode == 0:
            return snap_id, ref, commit
        last_exc = RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    raise RuntimeError(
        "could not claim a unique snapshot ref after {0} retries: {1}".format(
            MAX_REF_RETRIES, last_exc
        )
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def _id_seq(snap_id: str) -> int:
    """The persistent sequence field (NNNN) of an ID, or -1 if unparseable."""
    parts = snap_id.split("-")
    if len(parts) >= 3:
        try:
            return int(parts[2])
        except ValueError:
            pass
    return -1


def _refs_with_dates(repo: str) -> List[Tuple[int, int, str, str]]:
    """[(committer_epoch, seq, id, full_ref), ...] sorted newest first.

    Ordering key is (committer date, persistent seq), both descending. The old
    lexical sort on the ID string let the human-readable timestamp prefix
    dominate, so same-second captures tie-broke on PID rather than the monotonic
    seq; the seq now breaks same-second ties correctly and committerdate is
    robust to a seq reset. (Note: a backward wall-clock jump still reorders by
    committerdate -- committerdate IS the wall clock -- so this is not a full
    clock-rewind guarantee; it fixes the same-second tie and lexical-prefix
    dominance.)
    """
    out = gitutil.git_out(
        repo,
        [
            "for-each-ref",
            "--format=%(committerdate:unix)\t%(refname)",
            SNAP_REF_PREFIX,
        ],
    )
    rows = []
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        cdate_s, _sep, ref = line.partition("\t")
        if not ref:
            ref = cdate_s  # unexpected format; treat whole line as the ref
            cdate_s = "0"
        ref = ref.strip()
        try:
            cdate = int(cdate_s)
        except ValueError:
            cdate = 0
        snap_id = ref[len(SNAP_REF_PREFIX):]
        rows.append((cdate, _id_seq(snap_id), snap_id, ref))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return rows


def list_refs(repo: str) -> List[Tuple[str, str]]:
    """Return [(id, full_ref), ...] sorted newest first. See _refs_with_dates."""
    return [(snap_id, ref) for _c, _s, snap_id, ref in _refs_with_dates(repo)]


def snapshot_meta(repo: str, snap_id: str, ref: str) -> dict:
    commit = gitutil.git_out(repo, ["rev-parse", ref])
    # Subject + parent SHAs + committer epoch in one fork (%x00 = NUL separator,
    # %P = parents, %ct = committer unix time). A snapshot that captured a
    # differing staged index carries it as a parent, so a non-empty
    # parent list means a staged variant exists.
    info = gitutil.git_out(repo, ["log", "-1", "--format=%s%x00%P%x00%ct", ref])
    subject, _sep, rest = info.partition("\x00")
    parents, _sep2, ct = rest.partition("\x00")
    has_staged = bool(parents.strip())
    try:
        committer_epoch = int(ct.strip())
    except (ValueError, AttributeError):
        committer_epoch = 0
    # ``-z`` so the file count is correct even for names with newlines/tabs.
    raw_names = gitutil.run_git(
        repo, ["ls-tree", "-r", "--name-only", "-z", ref]
    ).stdout
    names = gitutil._decode_z(raw_names)
    label = ""
    via = ""
    # subject form: snapshot <id> [(label)] via=<via>
    if " via=" in subject:
        via = subject.rsplit(" via=", 1)[1].strip()
    if "(" in subject and ")" in subject:
        label = subject[subject.find("(") + 1: subject.rfind(")")]
    # bytes via cat-file batch on the tree (sum blob sizes)
    nbytes = _tree_bytes(repo, ref)
    return {
        "id": snap_id,
        "ref": ref,
        "commit": commit,
        "files": len(names),
        "bytes": nbytes,
        "label": label,
        "via": via,
        "has_staged": has_staged,
        # Prefer the commit's committer date (a true epoch, correct for both UTC
        # and pre-upgrade local-time IDs); fall back to the ID prefix only when
        # the commit has no parseable date (mirrors the prune path).
        "epoch": committer_epoch or ids.epoch_from_id(snap_id),
    }


def _tree_bytes(repo: str, ref: str) -> int:
    """Sum of blob sizes in the snapshot tree (long-form, NUL-delimited).

    ``-z`` keeps records intact for paths with embedded newlines/tabs. The path
    is not used here (we only sum sizes), but ``-z`` removes the line-split
    hazard that bit the non-``-z`` listings.
    """
    raw = gitutil.run_git(repo, ["ls-tree", "-r", "-l", "-z", ref]).stdout
    total = 0
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, _sep, _path = record.partition(b"\t")
        # <mode> SP <type> SP <sha> SP <size>
        parts = meta.split()
        if len(parts) >= 4 and parts[1] == b"blob":
            try:
                total += int(parts[3])
            except ValueError:
                pass
    return total


# ---------------------------------------------------------------------------
# Resolve / diff / restore
# ---------------------------------------------------------------------------


def resolve_ref(snap_id: str) -> str:
    if snap_id.startswith(SNAP_REF_PREFIX):
        return snap_id
    return SNAP_REF_PREFIX + snap_id


def ref_exists(repo: str, ref: str) -> bool:
    probe = gitutil.run_git(repo, ["rev-parse", "--verify", ref], check=False)
    return probe.returncode == 0


def _staged_ref(
    repo: str, ref: str, snap_id: str
) -> "Tuple[Optional[str], Optional[str]]":
    """Resolve the staged variant of a snapshot ref (its first parent).

    Returns ``(staged_ref, None)`` or ``(None, error_message)`` when the snapshot
    has no staged variant (its index matched the work tree at capture).
    """
    sref = ref + "^"
    if not ref_exists(repo, sref):
        return None, "snapshot {0} has no staged variant".format(snap_id)
    return sref, None


def diff_name_status(
    repo: str, id1: str, id2: Optional[str] = None, staged: bool = False
) -> Tuple[int, str]:
    """name-status diff between two snapshots, or snapshot vs work tree.

    ``staged=True`` diffs the staged-index variant of each snapshot.
    Returns (status, text). status 3 = unknown id / no staged variant.
    """
    ref1 = resolve_ref(id1)
    if not ref_exists(repo, ref1):
        return 3, "no such snapshot: {0}".format(id1)
    if staged:
        ref1, err = _staged_ref(repo, ref1, id1)
        if err:
            return 3, err
    if id2 is None:
        # Compare snapshot tree against the current work tree files.
        try:
            return 0, _diff_against_worktree(repo, ref1)
        except RuntimeError as exc:
            # e.g. git < 2.25 cannot build the work-tree diff; report rather than
            # silently show an empty (== "no differences") result.
            return 2, str(exc)
    ref2 = resolve_ref(id2)
    if not ref_exists(repo, ref2):
        return 3, "no such snapshot: {0}".format(id2)
    if staged:
        ref2, err = _staged_ref(repo, ref2, id2)
        if err:
            return 3, err
    out = gitutil.git_out(repo, ["diff", "--name-status", ref1, ref2])
    return 0, out


def _diff_against_worktree(repo: str, ref: str) -> str:
    """name-status of a snapshot tree vs the live work tree.

    Done with a private index so the user's index is never touched: read-tree the
    snapshot, then ``git add -A`` over a PATHSPEC that is exactly the capture
    model -- the secret/.snap-bak-filtered work-tree set (gitutil.list_worktree_files)
    unioned with the snapshot's own paths (so deletions of captured files still
    show). Restricting the pathspec this way means a secret is never hashed into
    the object DB nor shown as a phantom add: a plain ``git add -A`` over the
    whole tree wrote plaintext secret blobs into ``.git/objects`` on a read-only
    diff and listed them in the preview. Brand-new non-secret files
    still appear; modifications and deletions still appear.
    """
    snap = gitutil.snap_dir(repo)
    tmp_index = os.path.join(snap, "index.diff.{0}".format(os.getpid()))
    if os.path.exists(tmp_index):
        os.unlink(tmp_index)
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        gitutil.run_git(repo, ["read-tree", ref], extra_env=env)
        kept, _dropped = gitutil.list_worktree_files(repo)
        kept_set = set(kept)
        snap_paths = [rel for _m, _s, rel in _paths_under(repo, ref, "")]
        seen: set = set()
        pathspec: List[str] = []
        for rel in kept + snap_paths:
            if rel in seen or gitutil._is_snap_internal(rel):
                continue
            # A snapshot-side path that is an UNTRACKED secret must never enter the
            # pathspec: ``git add -A`` over it would hash the CURRENT secret
            # plaintext into ``.git/objects`` on a read-only diff.
            # ``kept`` already excludes untracked secrets (and exempts tracked
            # files), so a secret path survives here only if it is tracked.
            if rel not in kept_set and secret.is_secret(rel):
                continue
            seen.add(rel)
            pathspec.append(rel)
        if pathspec:
            payload = b"\x00".join(
                p.encode("utf-8", "surrogateescape") for p in pathspec
            ) + b"\x00"
            added = gitutil.run_git(
                repo,
                ["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                extra_env=env,
                input_bytes=payload,
                check=False,
            )
            if added.returncode != 0:
                # ``git add --pathspec-from-file``/``--pathspec-file-nul`` need git
                # >= 2.25. On older git the add fails and the temp index stays equal
                # to ``read-tree <ref>``, so the diff below would falsely report
                # ZERO differences -- a silent "nothing changed" during recovery.
                # Surface it loudly instead of swallowing it.
                raise RuntimeError(
                    "snapshot-vs-work-tree diff needs git >= 2.25 "
                    "(git add --pathspec-file-nul failed): {0}".format(
                        added.stderr.decode("utf-8", "replace").strip()
                    )
                )
        proc = gitutil.run_git(
            repo,
            ["diff", "--cached", "--name-status", ref],
            extra_env=env,
            check=False,
        )
        return proc.stdout.decode("utf-8", "replace").rstrip("\n")
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)


def _diff_file_against_worktree(repo: str, ref: str, path: str) -> str:
    """Unified diff of one snapshot file vs its live work-tree version.

    ``git diff <tree> -- <path>`` compares the tree against the INDEX, so for a
    path that is untracked in the user's real index it spuriously reports the
    file as deleted (``+++ /dev/null``) -- exactly the untracked files this tool
    exists to protect. Instead we read the snapshot into a private temp index and
    use ``diff-index -p <ref>`` (no ``--cached``), which diffs the WORK TREE
    against the tree, yielding the true old->new content diff.
    """
    snap = gitutil.snap_dir(repo)
    tmp_index = os.path.join(snap, "index.difffile.{0}".format(os.getpid()))
    if os.path.exists(tmp_index):
        os.unlink(tmp_index)
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        gitutil.run_git(repo, ["read-tree", ref], extra_env=env)
        proc = gitutil.run_git(
            repo,
            ["diff-index", "-p", ref, "--", path],
            extra_env=env,
            check=False,
        )
        return proc.stdout.decode("utf-8", "replace").rstrip("\n")
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)


def diff_file(
    repo: str, id1: str, path: str, id2: Optional[str] = None,
    staged: bool = False,
) -> Tuple[int, str]:
    """Unified diff of one file between snapshots (or snapshot vs work tree).

    ``staged=True`` diffs the staged-index variant of each snapshot.
    """
    ref1 = resolve_ref(id1)
    if not ref_exists(repo, ref1):
        return 3, "no such snapshot: {0}".format(id1)
    if staged:
        ref1, err = _staged_ref(repo, ref1, id1)
        if err:
            return 3, err
    if id2 is None:
        # snapshot vs work tree: must compare the snapshot blob to the live file,
        # not to the user's index (which would falsely show untracked files as
        # deleted).
        text = _diff_file_against_worktree(repo, ref1, path)
        if not text and _path_mode_sha_in_ref(repo, ref1, path)[0] is None:
            # Empty + not in the snapshot: distinguish from "identical" so the
            # user is not misled into thinking the file is captured.
            return 3, "path not in snapshot {0}: {1}".format(id1, path)
        return 0, text
    ref2 = resolve_ref(id2)
    if not ref_exists(repo, ref2):
        return 3, "no such snapshot: {0}".format(id2)
    if staged:
        ref2, err = _staged_ref(repo, ref2, id2)
        if err:
            return 3, err
    proc = gitutil.run_git(
        repo, ["diff", ref1, ref2, "--", path], check=False
    )
    return 0, proc.stdout.decode("utf-8", "replace").rstrip("\n")


def _safe_abs(repo: str, rel: str) -> str:
    """Resolve ``rel`` under ``repo``, rejecting only paths that ESCAPE the repo.

    The snapshot tree can only hold repo-relative, non-``..`` paths (git rejects
    the rest at write-tree time), so the danger is the *live* work tree at
    restore time: if a parent component of ``rel`` is now a symlink pointing
    outside the repo, a naive ``open(join(repo, rel))`` would write through it
    and clobber an arbitrary file. We reject absolute / ``..`` paths, then
    ``realpath`` the resolved parent (which follows EVERY symlinked component,
    intermediate or not) and confirm it is still contained in the repo.

    A symlinked parent that stays INSIDE the repo is allowed: blocking those too
    needlessly refused legitimate recovery into a symlinked subdirectory, and the
    realpath-containment check already blocks the only real hazard: an escaping
    symlink, which is still rejected.

    Residual race (accepted): the check and the later write are
    separate, so a co-process that turns a parent into an out-of-repo symlink
    between them could defeat the guard. That attacker can already write
    arbitrary files in the work tree directly, so this is out of the single-user
    dev-tool threat model; the static pre-existing-symlink case is fully guarded.
    """
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        raise ValueError("unsafe path (absolute or escaping): {0}".format(rel))
    abs_path = os.path.join(repo, rel)
    real_repo = os.path.realpath(repo)
    real_parent = os.path.realpath(os.path.dirname(abs_path))
    if real_parent != real_repo and not real_parent.startswith(real_repo + os.sep):
        raise ValueError("path escapes repo: {0}".format(rel))
    return abs_path


@contextmanager
def _restore_lock(repo: str) -> Iterator[bool]:
    """Hold the repo lock for the duration of a restore (best effort).

    Restoring under the lock means a concurrent capture (hook/preexec/manual)
    either rides along or skips instead of snapshotting a half-restored tree
    (capture treats ``LockBusy`` as a skip). But restore is user-initiated and
    must never hard fail because a brief background capture holds the lock, so
    on timeout we proceed without it and warn. Yields whether the lock is held.
    """
    lock = RepoLock(os.path.join(gitutil.shared_snap_dir(repo), "lock"))
    held = lock.acquire_blocking(timeout=_RESTORE_LOCK_TIMEOUT)
    if not held:
        sys.stderr.write(
            "  warning: snapshot lock busy; restoring without it\n"
        )
    try:
        yield held
    finally:
        if held:
            lock.release()


def _unique_backup_path(bak_path: str) -> str:
    """A non-existing backup path: ``bak_path`` itself, else ``bak_path.<n>``.

    Backups must never clobber an earlier backup of the same file -- a
    restore -> edit -> restore sequence would otherwise lose the content from
    before the FIRST restore (content that lives in no snapshot).
    """
    if not os.path.lexists(bak_path):
        return bak_path
    for i in range(1, 100000):
        cand = "{0}.{1}".format(bak_path, i)
        if not os.path.lexists(cand):
            return cand
    # Pathological fallback (100k existing backups of one path).
    return "{0}.{1}".format(bak_path, os.getpid())


def _snap_bak_unsafe(repo: str) -> Optional[str]:
    """Reason string if ``.snap-bak`` cannot be used safely for backups, else None.

    The common foot-gun is ``.snap-bak`` itself being a symlink (or resolving
    outside the repo): _backup_existing would then refuse EVERY file via _safe_abs
    and a ``restore --all`` would degrade to a silent per-file no-op for every
    existing file. Detect it once up front and report clearly so the user can
    remove/relocate it or pass ``--no-backup`` deliberately.
    """
    bak_root = os.path.join(repo, gitutil.SNAP_BAK_DIR)
    if os.path.islink(bak_root):
        return (
            "{0} is a symlink; remove/relocate it or restore with --no-backup"
            .format(gitutil.SNAP_BAK_DIR)
        )
    if os.path.exists(bak_root) and not os.path.isdir(bak_root):
        # ``.snap-bak`` exists but is a regular file (not a dir): ``_backup_existing``
        # would then ``makedirs`` under it and raise NotADirectoryError per file, so
        # ``restore --all`` would silently degrade to an all-skipped no-op. Report it
        # clearly up front like the symlink case.
        return (
            "{0} exists as a regular file; remove/relocate it or restore with "
            "--no-backup".format(gitutil.SNAP_BAK_DIR)
        )
    if os.path.exists(bak_root):
        real = os.path.realpath(bak_root)
        real_repo = os.path.realpath(repo)
        if real != real_repo and not real.startswith(real_repo + os.sep):
            return (
                "{0} resolves outside the repo; remove/relocate it or restore "
                "with --no-backup".format(gitutil.SNAP_BAK_DIR)
            )
    return None


def _backup_existing(repo: str, rel: str, no_backup: bool) -> Optional[str]:
    """Stash the current file to ``.snap-bak/<rel>`` before overwrite.

    Copies (never moves) so a failed restore write cannot lose the only copy.
    The copy streams via ``copyfileobj`` so a multi-GiB file does not balloon
    RSS. ``.snap-bak`` is excluded from capture so these backups
    are never re-snapshotted.

    Both the source AND the destination are validated by :func:`_safe_abs`: a
    symlinked component under ``.snap-bak`` must not redirect the user's current
    file outside the repo. Existing backups are never overwritten.
    """
    abs_path = _safe_abs(repo, rel)
    if no_backup or not os.path.lexists(abs_path):
        return None
    # Validate the destination with the same traversal / symlinked-parent guard
    # as the restore target, then pick a non-clobbering name.
    bak_path = _safe_abs(repo, os.path.join(gitutil.SNAP_BAK_DIR, rel))
    os.makedirs(os.path.dirname(bak_path), exist_ok=True)
    bak_path = _unique_backup_path(bak_path)
    if os.path.islink(abs_path):
        target = os.readlink(abs_path)
        os.symlink(target, bak_path)
    else:
        # Only a regular file can be streamed. A FIFO would BLOCK forever on
        # ``open(..., "rb")`` (it waits for a writer), hanging ``restore --all``;
        # a socket / device is meaningless to back up. Refuse non-regular nodes so
        # per-file isolation skips this one instead of hanging the whole restore.
        st = os.lstat(abs_path)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(
                "cannot back up {0}: not a regular file "
                "(FIFO/socket/device)".format(rel)
            )
        with open(abs_path, "rb") as src, open(bak_path, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 16)
    return bak_path


def _write_blob_to_path(repo: str, spec: str, rel: str, mode: str) -> None:
    """Write the blob identified by ``spec`` to ``rel`` in the work tree.

    ``spec`` is a blob SHA (bulk restore) or a ``ref:path`` rev (single file).
    Bulk restore passes the SHA from ``ls-tree -r -z`` so the path is never
    re-embedded into a rev-spec -- that re-introduced the C-quoting / resolution
    failure that aborted whole batches on odd names. ``rel`` is validated by
    :func:`_safe_abs` (traversal / symlinked-parent guard).
    """
    abs_path = _safe_abs(repo, rel)
    parent = os.path.dirname(abs_path)
    if parent:
        # exist_ok: under the lock-busy degraded restore path two restores can
        # race to create the same new parent; without it the loser hit
        # FileExistsError and skipped a file unnecessarily.
        os.makedirs(parent, exist_ok=True)
    if mode == "120000":
        # Symlink: the blob is the (small) target string. Create at a temp name
        # then atomically replace, so a crash never leaves the path missing the
        # way unlink-then-symlink could.
        blob = gitutil.run_git(repo, ["cat-file", "blob", spec], check=False)
        if blob.returncode != 0:
            raise RuntimeError("could not read {0} from snapshot".format(rel))
        target = blob.stdout.decode("utf-8", "surrogateescape")
        tmp = abs_path + gitutil.SNAP_RESTORE_TMP_SUFFIX
        if os.path.lexists(tmp):
            os.unlink(tmp)
        try:
            os.symlink(target, tmp)
            os.replace(tmp, abs_path)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        return
    # Regular file: stream cat-file straight to a temp file, then atomically
    # replace (RSS stays O(64 KiB) even for multi-GiB blobs; replace
    # avoids leaving a half-written file if git or the disk fails mid-restore).
    tmp = abs_path + gitutil.SNAP_RESTORE_TMP_SUFFIX
    # A leftover tmp from an interrupted restore could be a SYMLINK; ``open(tmp,
    # "wb")`` would follow it and clobber an arbitrary out-of-repo target. Remove
    # any existing tmp first (the symlink branch above already does this).
    if os.path.lexists(tmp):
        os.unlink(tmp)
    try:
        with open(tmp, "wb") as fh:
            proc = gitutil.run_git_to_file(repo, ["cat-file", "blob", spec], fh)
        if proc.returncode != 0:
            raise RuntimeError("could not read {0} from snapshot".format(rel))
        if mode == "100755":
            st = os.stat(tmp)
            os.chmod(
                tmp, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        os.replace(tmp, abs_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _path_mode_sha_in_ref(
    repo: str, ref: str, rel: str
) -> Tuple[Optional[str], Optional[str]]:
    """``(mode, blob_sha)`` of a path in a snapshot, or ``(None, None)``."""
    lstree = gitutil.run_git(repo, ["ls-tree", ref, "--", rel], check=False)
    if lstree.returncode != 0 or not lstree.stdout.strip():
        return None, None
    # surrogateescape, not strict: with ``core.quotePath=false`` (always set, see
    # gitutil._GLOBAL_CONFIG) a non-UTF-8 path comes back as raw bytes, so a
    # strict decode here would crash single-file restore / diff. Only
    # the leading mode + sha tokens are used; both are ASCII and unaffected.
    parts = lstree.stdout.decode("utf-8", "surrogateescape").split()
    # form: <mode> {blob|commit} <sha>\t<path>
    if len(parts) < 3:
        return None, None
    return parts[0], parts[2]


def _current_differs(repo: str, rel: str, snap_sha: str) -> bool:
    """True when the live work-tree file differs from the snapshot blob.

    Used only to *warn* before overwriting. Hashes without ``-w`` so it
    never writes to the object DB. Best-effort: any error means "no warning".
    """
    abs_path = os.path.join(repo, rel)
    if not os.path.lexists(abs_path):
        return False
    # A FIFO/socket/device at the live path would BLOCK ``git hash-object`` (FIFO
    # open-for-read) until the timeout; this is only a best-effort drift warning,
    # so treat a non-regular/non-symlink node as "no warning" rather than hang.
    try:
        st = os.lstat(abs_path)
    except OSError:
        return False
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        return False
    try:
        if os.path.islink(abs_path):
            target = os.readlink(abs_path).encode("utf-8", "surrogateescape")
            cur = gitutil.run_git(
                repo, ["hash-object", "--stdin"], input_bytes=target
            ).stdout.decode("utf-8").strip()
        else:
            cur = gitutil.git_out(
                repo, ["hash-object", "--no-filters", abs_path]
            )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False
    return cur != snap_sha


def restore_file(
    repo: str, snap_id: str, path: str, no_backup: bool = False,
    staged: bool = False,
) -> Tuple[int, str]:
    """Restore one file. status 3 = id/path not in snapshot.

    ``staged=True`` restores from the snapshot's staged-index variant.
    """
    ref = resolve_ref(snap_id)
    if not ref_exists(repo, ref):
        return 3, "no such snapshot: {0}".format(snap_id)
    if staged:
        ref, err = _staged_ref(repo, ref, snap_id)
        if err:
            return 3, err
    mode, snap_sha = _path_mode_sha_in_ref(repo, ref, path)
    if mode is None:
        return 3, "file not in snapshot {0}: {1}".format(snap_id, path)
    if mode == "040000":
        # The path is a DIRECTORY in the snapshot, not a file. Without this guard
        # restore would feed the tree to _backup_existing/_write_blob_to_path and
        # surface a cryptic "[Errno 21] Is a directory" / "could not read" error;
        # tell the user the right command instead.
        return 2, (
            "{0} is a directory in snapshot {1}; restore it with "
            "'restore {1} --dir {0}'".format(path, snap_id)
        )
    if mode == "160000":
        # Submodule gitlink: surface the pin, don't auto-mutate the submodule
        # (that would touch its HEAD and could itself lose work).
        _warn_submodules([(path, snap_sha)])
        return 0, "submodule {0}: pinned commit {1} (restore manually)".format(
            path, snap_sha
        )
    if not no_backup:
        reason = _snap_bak_unsafe(repo)
        if reason:
            return 2, reason
    # Surface (don't silently clobber) local changes. The pre-restore
    # content is preserved in .snap-bak; we just tell the user it happened.
    drift = _current_differs(repo, path, snap_sha) if not no_backup else False
    bak_path: Optional[str] = None
    try:
        with _restore_lock(repo):
            # Capture the ACTUAL backup path so the drift note below is only
            # printed when a backup was really written -- a TOCTOU (file deleted
            # between the drift check and the backup) must not claim a backup that
            # does not exist.
            bak_path = _backup_existing(repo, path, no_backup)
            # Restore by blob SHA (not ref:path) so odd names always resolve.
            _write_blob_to_path(repo, snap_sha, path, mode)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return 2, "could not restore {0}: {1}".format(path, exc)
    if drift and bak_path is not None:
        sys.stderr.write(
            "  note: {0} had local changes differing from the snapshot; the "
            "pre-restore version was backed up to {1}\n".format(path, bak_path)
        )
    return 0, "restored {0} from snapshot {1}".format(path, snap_id)


def _norm_subtree(subtree: str) -> str:
    """Normalise a ``--dir`` subtree to a snapshot-path prefix.

    ``os.path.normpath`` collapses ``.``/``./`` (and ``a/../b``) so the natural
    "restore the repo root" invocations ``--dir .`` and ``--dir ./`` map to the
    whole-tree sentinel ``""`` instead of the literal ``.`` -- which matched no
    stored path and silently restored nothing.
    """
    prefix = os.path.normpath(subtree).strip("/")
    return "" if prefix == "." else prefix


def _paths_under(repo: str, ref: str, subtree: str) -> List[Tuple[str, str, str]]:
    """[(mode, blob_sha, path)] of blobs under a subtree prefix in the snapshot.

    Uses ``ls-tree -r -z`` so paths with newlines / tabs / non-ASCII bytes come
    back verbatim (NUL-delimited, never C-quoted) and returns each blob's SHA so
    the caller restores by object id -- never by re-embedding the path into a
    ``ref:path`` rev-spec (which re-introduced the quoting failure that aborted
    whole batches). An empty ``subtree`` means the whole tree (``restore --all``).
    """
    raw = gitutil.run_git(repo, ["ls-tree", "-r", "-z", ref]).stdout
    prefix = _norm_subtree(subtree)
    all_paths = prefix == ""
    results: List[Tuple[str, str, str]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, sep, path_b = record.partition(b"\t")
        if not sep:
            continue
        parts = meta.split()  # <mode> <type> <sha>
        if len(parts) < 3 or parts[1] != b"blob":
            continue
        mode = parts[0].decode("ascii")
        sha = parts[2].decode("ascii")
        rel = path_b.decode("utf-8", "surrogateescape")
        if all_paths or rel == prefix or rel.startswith(prefix + "/"):
            results.append((mode, sha, rel))
    return results


def _gitlinks_under(repo: str, ref: str, subtree: str) -> List[Tuple[str, str]]:
    """[(path, commit_sha)] of submodule gitlinks under a subtree.

    Gitlinks (mode 160000, ``ls-tree`` type ``commit``) are not blobs, so
    :func:`_restore_entries` cannot write them. Restore surfaces their pinned
    commit so the user can check the submodule out manually rather than having
    the pin vanish.
    """
    raw = gitutil.run_git(repo, ["ls-tree", "-r", "-z", ref]).stdout
    prefix = _norm_subtree(subtree)
    all_paths = prefix == ""
    results: List[Tuple[str, str]] = []
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, sep, path_b = record.partition(b"\t")
        if not sep:
            continue
        parts = meta.split()  # <mode> <type> <sha>
        if len(parts) < 3 or parts[1] != b"commit":
            continue
        sha = parts[2].decode("ascii")
        rel = path_b.decode("utf-8", "surrogateescape")
        if all_paths or rel == prefix or rel.startswith(prefix + "/"):
            results.append((rel, sha))
    return results


def _warn_submodules(gitlinks: List[Tuple[str, str]]) -> None:
    """Tell the user about submodule pins that restore does not auto-apply."""
    for rel, sha in gitlinks:
        sys.stderr.write(
            "  note: {0} is a submodule; snapshot pinned commit {1}. It is not "
            "auto-restored -- check it out manually: "
            "git -C {0} checkout {1}\n".format(rel, sha)
        )


def _restore_entries(
    repo: str, entries: List[Tuple[str, str, str]], no_backup: bool
) -> Tuple[int, int]:
    """Restore each ``(mode, sha, rel)`` with per-file isolation, under the lock.

    One bad target -- an unreadable blob, an unsafe / symlinked path, an
    un-writable parent -- is warned-and-skipped, never aborting the rest. This
    mirrors the capture side's "one bad file never stops the snapshot" guarantee
    on the restore side (previously a single failure aborted the whole batch and
    left every later file unrestored). Returns ``(restored, skipped)``.
    """
    restored = 0
    skipped = 0
    total = len(entries)
    with _restore_lock(repo):
        for mode, sha, rel in entries:
            try:
                _backup_existing(repo, rel, no_backup)
                _write_blob_to_path(repo, sha, rel, mode)
                restored += 1
            except (ValueError, RuntimeError, OSError,
                    subprocess.TimeoutExpired) as exc:
                sys.stderr.write(
                    "  warning: skipped {0} during restore ({1})\n".format(
                        rel, exc
                    )
                )
                skipped += 1
            except KeyboardInterrupt:
                # The batch is not atomic (each file is, via os.replace, but the
                # set is not). Tell the user exactly how far we got so they can
                # recover, then propagate. The lock is released by the
                # context manager as the exception unwinds.
                sys.stderr.write(
                    "\n  interrupted: restored {0} of {1} file(s) -- re-run the "
                    "same restore to finish (already-restored files are "
                    "idempotent; any overwritten originals are saved under "
                    "{2}/)\n".format(restored, total, gitutil.SNAP_BAK_DIR)
                )
                raise
    return restored, skipped


def _restore_summary(
    restored: int, skipped: int, scope: str, snap_id: str, backed_up: bool = False
) -> str:
    msg = "restored {0} file(s) {1} from {2}".format(restored, scope, snap_id)
    if skipped:
        msg += " ({0} skipped)".format(skipped)
    if backed_up and restored:
        # Surface the recoverability of any overwritten work so a bulk
        # restore --dir / --all is not silently destructive.
        msg += "; any overwritten files were saved under {0}/".format(
            gitutil.SNAP_BAK_DIR
        )
    return msg


def _restore_status(restored: int, entries: List) -> int:
    """0 when at least one file restored; 2 when every entry was skipped.

    Per-file isolation keeps a mixed batch going, but a batch where *nothing*
    restored (all blobs corrupt, or all paths refused by the symlink-parent
    guard) must not report success to scripts/users.
    """
    return 0 if (restored > 0 or not entries) else 2


def restore_dir(
    repo: str, snap_id: str, subtree: str, no_backup: bool = False,
    staged: bool = False,
) -> Tuple[int, str]:
    """Restore every file under a subtree from a snapshot.

    ``staged=True`` restores from the snapshot's staged-index variant.
    """
    ref = resolve_ref(snap_id)
    if not ref_exists(repo, ref):
        return 3, "no such snapshot: {0}".format(snap_id)
    if staged:
        ref, err = _staged_ref(repo, ref, snap_id)
        if err:
            return 3, err
    entries = _paths_under(repo, ref, subtree)
    gitlinks = _gitlinks_under(repo, ref, subtree)
    if not entries and not gitlinks:
        return 3, "no files under {0} in snapshot {1}".format(subtree, snap_id)
    if not no_backup:
        reason = _snap_bak_unsafe(repo)
        if reason:
            return 2, reason
    restored, skipped = _restore_entries(repo, entries, no_backup)
    _warn_submodules(gitlinks)
    return (
        _restore_status(restored, entries),
        _restore_summary(
            restored, skipped, "under {0}".format(subtree), snap_id,
            backed_up=not no_backup,
        ),
    )


def restore_all(
    repo: str, snap_id: str, no_backup: bool = False, staged: bool = False
) -> Tuple[int, str]:
    """Restore every file in a snapshot (destructive; caller must confirm).

    ``staged=True`` restores from the snapshot's staged-index variant.
    """
    ref = resolve_ref(snap_id)
    if not ref_exists(repo, ref):
        return 3, "no such snapshot: {0}".format(snap_id)
    if staged:
        ref, err = _staged_ref(repo, ref, snap_id)
        if err:
            return 3, err
    entries = _paths_under(repo, ref, "")
    gitlinks = _gitlinks_under(repo, ref, "")
    if not no_backup:
        reason = _snap_bak_unsafe(repo)
        if reason:
            return 2, reason
    restored, skipped = _restore_entries(repo, entries, no_backup)
    _warn_submodules(gitlinks)
    return (
        _restore_status(restored, entries),
        _restore_summary(
            restored, skipped, "(all)", snap_id, backed_up=not no_backup
        ),
    )


# ---------------------------------------------------------------------------
# Prune / GC
# ---------------------------------------------------------------------------


def prune(
    repo: str,
    keep_generations: int = 50,
    max_bytes: int = 512 * 1024 * 1024,
    keep_hours: int = 72,
    dry_run: bool = False,
    run_gc: bool = True,
) -> dict:
    """Apply retention policy and (unless dry-run) reclaim objects.

    Policy union: a ref is dropped if it exceeds the generation cap OR pushes
    cumulative (deduplicated) bytes over ``max_bytes`` OR is older than
    ``keep_hours``. The newest snapshot (index 0) is ALWAYS kept by every policy
    so the safety net can never delete its own most-recent capture -- even with
    ``keep_generations=0`` or when all snapshots are older than ``keep_hours``;
    and, under a backward clock jump, the highest-seq ref is also always kept.

    The ``run_gc`` step runs a ``git gc`` that pins a 2-week prune grace
    (``gc.pruneExpire=2.weeks.ago``) on the command line so an aggressive user
    config cannot force-prune, so the user's own unreachable objects are not
    collaterally destroyed. Returns a summary dict.
    """
    lock_path = os.path.join(gitutil.shared_snap_dir(repo), "lock")
    lock = RepoLock(lock_path)
    try:
        lock.acquire()
    except LockBusy:
        return {
            "pruned": 0,
            "kept": len(list_refs(repo)),
            "reclaimed_loose": 0,
            "reason": "lock busy; skipped",
        }
    try:
        result, should_gc = _prune_locked(
            repo,
            keep_generations,
            max_bytes,
            keep_hours,
            dry_run,
            run_gc,
        )
    finally:
        lock.release()
    # Run gc AFTER releasing the shared lock. git gc can run for up to
    # GC_TIMEOUT (600s); holding the lock across it made a protective capture the
    # instant before a destructive command give up after _CAPTURE_LOCK_TIMEOUT
    # (3s) and skip the snapshot -- a data-loss window opened BY the safety net's
    # own housekeeping. The lock only needs to serialise the ref decisions +
    # deletions in _prune_locked; the gc that follows is plain repo housekeeping
    # that git serialises itself (gc.pid). The brief TOCTOU this opens (a
    # concurrent capture's reused blob pruned between its phase-1.5 existence check
    # and write-tree) is self-healing: write-tree fails, capture fails-open, and
    # the next capture demotes the dead SHA to a re-hash. gc prunes only
    # objects BOTH older than 2 weeks AND unreachable, and a fresh snapshot's
    # blobs are reachable, so the window is vanishingly small.
    if should_gc:
        result["reclaimed_loose"] = _run_gc_reclaim(repo)
    return result


def _loose_object_count(repo: str) -> int:
    out = gitutil.git_out(repo, ["count-objects"])
    # form: "<n> objects, <k> kilobytes"
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def _tree_blobs(repo: str, ref: str) -> Dict[str, int]:
    """``{blob_sha: size}`` for every blob in the snapshot tree.

    Unlike :func:`_tree_bytes` (a logical sum), this keys by object id so a blob
    shared across snapshots -- which git deduplicates on disk -- can be counted
    once toward the ``max_bytes`` retention policy instead of N times.
    """
    raw = gitutil.run_git(repo, ["ls-tree", "-r", "-l", "-z", ref]).stdout
    blobs: Dict[str, int] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        meta, _sep, _path = record.partition(b"\t")
        # <mode> SP <type> SP <sha> SP <size>
        parts = meta.split()
        if len(parts) >= 4 and parts[1] == b"blob":
            sha = parts[2].decode("ascii")
            try:
                blobs[sha] = int(parts[3])
            except ValueError:
                pass
    return blobs


def _prune_locked(
    repo: str,
    keep_generations: int,
    max_bytes: int,
    keep_hours: int,
    dry_run: bool,
    run_gc: bool,
) -> "Tuple[dict, bool]":
    """Decide + delete refs under the shared lock; return (summary, should_gc).

    The gc itself is deliberately NOT run here -- :func:`prune` runs it after
    releasing the lock so a protective capture is not blocked for up to GC_TIMEOUT
    ``reclaimed_loose`` is filled by that post-lock gc.
    """
    rows = _refs_with_dates(repo)  # [(committer_epoch, seq, id, ref)] newest first
    now = int(time.time())

    # Forward clock jump: a snapshot captured while the clock was set FAR ahead is
    # stamped in the future, so sorted by committerdate it masquerades as "newest"
    # -- it occupies the always-kept idx-0 slot and the top generation slots, and
    # once the clock is corrected every genuine snapshot (now stamped "earlier")
    # falls past keep_generations and is pruned, leaving only the bogus future ref
    # plus the seq-newest (data loss; forward case). Demote
    # future-stamped refs (beyond a clock-skew margin) to the OLDEST position so
    # they are pruned FIRST and genuine refs keep their slots; seq breaks ties.
    if rows:
        rows.sort(
            key=lambda r: (r[0] <= now + _CLOCK_SKEW_MARGIN, r[0], r[1]),
            reverse=True,
        )

    cutoff = now - keep_hours * 3600 if keep_hours >= 0 else None

    # rows[0] is "newest by committerdate", but a backward system-clock jump at
    # capture time stamps the genuinely-newest snapshot with an EARLIER date,
    # sinking it below older refs -- so pruning by date / generation / size could
    # drop the very last line of defence. The persistent
    # seq counter is monotonic and clock-independent, so also protect the
    # highest-seq ref. In the normal (no-rewind) case that IS rows[0], so nothing
    # extra is kept; under a rewind we keep rows[0] AND the seq-newest -- one
    # extra ref, the safe direction. (Residual: a seq wrap at 10000 within one
    # un-pruned set, or a lost seq file concurrent with a rewind -- both far
    # rarer than the bug this closes.)
    seq_newest_ref = max(rows, key=lambda r: r[1])[3] if rows else None

    to_drop: List[str] = []
    cumulative = 0
    counted_shas: set = set()  # blobs already charged to cumulative (dedup)
    size_exceeded = False      # size policy is a suffix: once over, drop the rest
    for idx, (cdate, _seq, snap_id, ref) in enumerate(rows):
        blobs = _tree_blobs(repo, ref)

        def _charge():
            # Charge this ref's not-yet-seen blobs to cumulative ONCE; git stores
            # a shared blob a single time, so summing logical per-snapshot sizes
            # over-counts.
            nonlocal cumulative
            for sha, size in blobs.items():
                if sha not in counted_shas:
                    counted_shas.add(sha)
                    cumulative += size

        # The newest snapshot is the safety net's last line of defence and is
        # ALWAYS kept by every retention policy: both the
        # committerdate-newest (idx 0) AND the seq-newest (clock-rewind robust).
        # A kept ref still counts toward the byte budget
        # so older refs are measured against the true kept footprint
        # (previously its bytes were omitted, under-enforcing max_bytes).
        if idx == 0 or ref == seq_newest_ref:
            _charge()
            continue

        drop = False
        if keep_generations >= 0 and idx >= keep_generations:
            drop = True
        # Age: use the commit's date (a true epoch, correct for both UTC and
        # pre-upgrade local-time IDs), falling back to the ID prefix.
        if cutoff is not None:
            epoch = cdate or ids.epoch_from_id(snap_id)
            if epoch and epoch < cutoff:
                drop = True
            # A forward clock jump stamps a bogus ref in the FUTURE; under an
            # age-only policy (keep_generations < 0) its cdate > cutoff, so it
            # would never expire and would pollute the set permanently -- the
            # generation-policy demotion above does not reach it. Treat a clearly-
            # future cdate as an age-policy drop too. The idx==0 / seq_newest_ref
            # guard above still protects the genuinely-newest ref.
            if cdate and cdate > now + _CLOCK_SKEW_MARGIN:
                drop = True
        # Size: only CHARGE a ref's bytes to the budget when it is being KEPT --
        # bytes of a ref dropped for age/generation must not push kept older refs
        # over the cap. Once the cap is crossed, every older ref
        # is dropped too (suffix semantics).
        if max_bytes >= 0 and not drop:
            if size_exceeded:
                drop = True
            else:
                add = sum(
                    size for sha, size in blobs.items() if sha not in counted_shas
                )
                if cumulative + add > max_bytes:
                    drop = True
                    size_exceeded = True
                else:
                    _charge()
        if drop:
            to_drop.append(ref)

    if dry_run:
        return {
            "pruned": len(to_drop),
            "kept": len(rows) - len(to_drop),
            "reclaimed_loose": 0,
            "dropped_refs": to_drop,
            "dry_run": True,
        }, False

    for ref in to_drop:
        gitutil.run_git(repo, ["update-ref", "-d", ref], check=False)

    return {
        "pruned": len(to_drop),
        "kept": len(rows) - len(to_drop),
        "reclaimed_loose": 0,   # filled by the post-lock gc in prune()
        "dropped_refs": to_drop,
        "dry_run": False,
    }, bool(run_gc and to_drop)


def _run_gc_reclaim(repo: str) -> int:
    """Run the retention gc and return a rough loose-object delta.

    Called by :func:`prune` AFTER the shared lock is released so a
    protective capture is never blocked for up to GC_TIMEOUT.

    The grace is PINNED on the command line, not inherited from config. We
    deliberately do NOT pass --prune=now or a tight --prune=<keep_hours>.hours.ago:
    this gc is repo-wide and would otherwise obliterate the USER's own
    unreachable-but-recoverable objects (dropped stashes, pre-reset commits,
    reflog-expired work) as a side effect of snapshot retention. Dropped snapshot
    objects are reclaimed on the normal grace schedule.

    Object reclaim is governed SOLELY by the ``gc.pruneExpire=2.weeks.ago`` pin so
    the README promise ("never force-prunes your own objects") holds even under a
    common ``gc.pruneExpire=now`` (which would otherwise force-prune the user's
    dangling work).

    Reflog expiry is pinned to ``never`` in BOTH directions, so our retention gc
    never touches the user's reflog: it neither lets an aggressive
    ``reflogExpireUnreachable=now`` wipe their dangling work during our gc,
    nor shortens a long-retention user's ``reflogExpire=never`` down to a default
    (the bug an earlier ``=90.days`` / ``=30.days`` pin introduced).
    Reclaiming our OWN dropped snapshots does not need reflog expiry: ``update-ref
    -d`` already removes each deleted ref's reflog, so those objects are
    unreachable and pruned on the pruneExpire schedule regardless.
    """
    before_loose = _loose_object_count(repo)
    gitutil.run_git(
        repo,
        [
            "-c", "gc.pruneExpire=2.weeks.ago",
            "-c", "gc.reflogExpire=never",
            "-c", "gc.reflogExpireUnreachable=never",
            "gc", "--quiet",
        ],
        check=False,
        timeout=gitutil.GC_TIMEOUT,
    )
    after_loose = _loose_object_count(repo)
    # Loose objects can drop because they were PACKED (still present), not only
    # pruned -- so this is "loose objects packed or pruned", a rough housekeeping
    # signal, not a true bytes-reclaimed figure.
    return max(0, before_loose - after_loose)
