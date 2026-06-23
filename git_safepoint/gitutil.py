#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin wrappers around git plumbing used by the snapshot engine.

All git invocation in the package funnels through here so behaviour (env,
encoding, error handling) stays consistent. Standard library only.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from . import secret

SNAP_REF_PREFIX = "refs/snapshots/"

# Bound every git call so a stalled network FS / lingering index.lock / runaway
# gc cannot hang the protective snapshot forever and block the agent's command --
# "fail-open" only covers exceptions, not hangs. Override via env; gc
# legitimately runs longer than a capture fork so it gets a larger ceiling.
GIT_TIMEOUT = float(os.environ.get("GIT_SAFEPOINT_GIT_TIMEOUT", "120"))
GC_TIMEOUT = float(os.environ.get("GIT_SAFEPOINT_GC_TIMEOUT", "600"))
# Restore streams a whole blob to disk and is user-initiated (not the
# block-the-agent path), so a legitimately large blob on a slow mount must not be
# truncated by the tight capture ceiling -- give it a much larger bound that
# still prevents an infinite hang.
RESTORE_TIMEOUT = float(os.environ.get("GIT_SAFEPOINT_RESTORE_TIMEOUT", "3600"))

# Git's own repo/index-selection environment variables. They OVERRIDE ``git -C
# repo`` -- an inherited ``GIT_DIR`` makes every call operate on a *different*
# repository, and an inherited ``GIT_INDEX_FILE`` makes the read-side enumeration
# (``ls-files``) read the wrong index, silently reclassifying tracked files as
# untracked or capturing an empty tree. git itself exports these into any hook
# subprocess it spawns (during rebase/merge/reset -- the very moments this tool
# treats as destructive), so they routinely leak into our environment. We scrub
# them on every invocation; the engine re-applies its own private GIT_INDEX_FILE
# via ``extra_env`` (which runs last), so the capture/diff temp indexes still win.
_GIT_CONTEXT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    # Config-injection vars: an inherited GIT_CONFIG_COUNT lets arbitrary config
    # (core.hooksPath, core.fsmonitor, alias.*, include.path, ...) be injected
    # into every git call; GIT_CONFIG_GLOBAL/SYSTEM redirect the config files.
    # Scrub them too (defense-in-depth). GIT_CONFIG_PARAMETERS is the
    # older sibling git itself exports when it runs a subprocess under ``-c
    # key=val`` (rebase/merge/commit hooks), so it leaks into our environment the
    # same way GIT_DIR does and injects config just like GIT_CONFIG_COUNT --
    # scrub it on the same footing.
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_PARAMETERS",
    # commit-tree honours GIT_AUTHOR_DATE / GIT_COMMITTER_DATE. If either leaks
    # in (a shell that exports them for reproducible builds, or git itself when
    # the snapshot runs from inside a commit/rebase/am hook), the snapshot commit
    # is stamped with an arbitrary date while its ID prefix still reads the real
    # "now". prune's age policy then drops a snapshot taken seconds ago, and the
    # committerdate ordering / dedup are skewed. Scrub them so commit-tree always
    # stamps wall-clock now (the snapshot's true capture time).
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_DATE",
    # Pathspec-interpretation vars git exports to hooks. An inherited value would
    # change how the ``-- <path>`` operand is parsed in single-file ls-tree / diff
    # (e.g. a literal filename with glob metacharacters matching the wrong file).
    # Restore is immune (it lists with ``-r`` and writes by blob SHA), but scrub
    # them so diff output is deterministic regardless of the environment.
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_LITERAL_PATHSPECS",
)


def _child_env(extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """A child-process env with git's repo-selection overrides scrubbed.

    ``extra_env`` is applied AFTER the scrub so the engine's private
    GIT_INDEX_FILE (capture / diff temp index) still takes effect.
    """
    env = os.environ.copy()
    for key in _GIT_CONTEXT_ENV:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return env

# Global git config injected on EVERY invocation. ``core.quotePath=false`` makes
# git emit non-ASCII paths verbatim (UTF-8) instead of C-quoting them
# (``"caf\303\251.txt"``). Without it, any listing/diff that is parsed back into
# a real path (ls-tree, diff, diff-index) breaks for non-ASCII names. ``-z``
# output is already verbatim, so this is harmless there; it only fixes the
# human-readable / line-parsed forms. (Embedded newlines/tabs still require
# ``-z`` -- quotePath alone does not help line-based parsing.)
_GLOBAL_CONFIG = ["-c", "core.quotePath=false"]

# Restore writes pre-overwrite copies here (work-tree relative). It must never
# be captured, or every restore would re-snapshot the previous backup and the
# object store would grow recursively. Excluded like the secret floor.
SNAP_BAK_DIR = ".snap-bak"

# Restore streams each blob to ``<target>.snap-restore-tmp`` then atomically
# replaces. A hard kill mid-restore can leave one of these behind; it must never
# be captured (it would be snapshotted as noise, then restored as a stray file).
SNAP_RESTORE_TMP_SUFFIX = ".snap-restore-tmp"


def _is_snap_internal(path: str) -> bool:
    """True for git-safepoint's own work-tree artifacts that must not be captured."""
    return (
        path == SNAP_BAK_DIR
        or path.startswith(SNAP_BAK_DIR + "/")
        or path.endswith(SNAP_RESTORE_TMP_SUFFIX)
    )


class NotARepoError(Exception):
    """Raised when a path is not inside a git work tree."""


def run_git(
    repo: str,
    args: List[str],
    extra_env: Optional[Dict[str, str]] = None,
    input_bytes: Optional[bytes] = None,
    check: bool = True,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``git -C repo <args>`` and return the completed process.

    ``timeout`` defaults to :data:`GIT_TIMEOUT`; on expiry ``subprocess`` kills
    the child and raises ``TimeoutExpired`` so a hung git never blocks forever.
    Pass an explicit value (e.g. :data:`GC_TIMEOUT`) for long ops.
    """
    env = _child_env(extra_env)
    cmd = ["git", "-C", repo] + _GLOBAL_CONFIG + args
    return subprocess.run(
        cmd,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        timeout=GIT_TIMEOUT if timeout is None else timeout,
    )


def git_out(
    repo: str, args: List[str], extra_env: Optional[Dict[str, str]] = None
) -> str:
    """Run git and return stdout decoded, with the trailing newline stripped.

    Decodes with ``surrogateescape`` (not strict UTF-8) so a snapshot containing
    a filename with raw non-UTF-8 bytes -- which ``core.quotePath=false`` makes
    git emit verbatim -- never raises ``UnicodeDecodeError`` mid-command (e.g. a
    snapshot-vs-snapshot ``diff --name-status``).
    """
    return run_git(repo, args, extra_env=extra_env).stdout.decode(
        "utf-8", "surrogateescape"
    ).rstrip("\n")


def is_work_tree(repo: str) -> bool:
    """True when ``repo`` is inside a git work tree."""
    inside = run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode != 0:
        return False
    return inside.stdout.decode("utf-8").strip() == "true"


def git_dir(repo: str) -> str:
    """Absolute path to the real .git directory for this work tree.

    Honours worktrees / submodules via ``rev-parse --absolute-git-dir``.
    """
    out = git_out(repo, ["rev-parse", "--absolute-git-dir"])
    return out


def git_path(repo: str, rel: str) -> str:
    """Resolve a path inside the git dir (handles worktrees).

    ``git rev-parse --git-path snap/`` returns the real on-disk location even
    when a linked worktree is in use.
    """
    out = git_out(repo, ["rev-parse", "--git-path", rel])
    if os.path.isabs(out):
        return out
    return os.path.join(repo, out)


def snap_dir(repo: str) -> str:
    """The PER-WORKTREE .git/snap directory (created on demand), worktree-safe.

    Holds the mtime cache and capture/diff temp indexes -- state that is
    legitimately specific to one worktree's own files. ``git rev-parse
    --git-path snap`` returns the per-worktree gitdir for a linked worktree
    (``.git/worktrees/<name>/snap``). Cross-worktree shared state (the lock and
    the seq counter) lives in :func:`shared_snap_dir` instead.

    Forced to mode 0o700: the sidecar state here (``mtime-cache.json``'s
    path+SHA inventory, temp indexes) is written outside git's own permission
    logic, so it must be owner-only regardless of umask /
    ``core.sharedRepository``.
    """
    return _ensure_snap(git_path(repo, "snap"))


def git_common_dir(repo: str) -> str:
    """Absolute path to the COMMON git dir, shared across all linked worktrees.

    ``--git-common-dir`` returns the main gitdir even from a linked worktree
    (where ``--git-path`` would return the per-worktree dir). It may be relative
    (``.git`` for the main worktree), so resolve it against ``repo``.
    """
    out = git_out(repo, ["rev-parse", "--git-common-dir"])
    return out if os.path.isabs(out) else os.path.join(repo, out)


def shared_snap_dir(repo: str) -> str:
    """The SHARED .git/snap directory on the common git dir (created on demand).

    Holds the per-repo ``lock`` and ``seq`` counter so they serialise / stay
    monotonic across ALL linked worktrees of a repo: ``refs/snapshots/*`` and the
    object store are shared, so per-worktree lock/seq files would give no mutual
    exclusion and a non-monotonic seq (defeating prune's clock-rewind guard).
    For the main worktree this is the same ``.git/snap`` as :func:`snap_dir`, so
    single-worktree users see no change.
    """
    return _ensure_snap(os.path.join(git_common_dir(repo), "snap"))


def _ensure_snap(path: str) -> str:
    """makedirs + owner-only chmod; return the path."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _decode_z(raw: bytes) -> List[str]:
    """Split a ``-z`` (NUL-separated) git listing into decoded rel paths."""
    out: List[str] = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        out.append(chunk.decode("utf-8", "surrogateescape"))
    return out


def _matches_any(rel: str, patterns: List[str]) -> bool:
    """True when ``rel`` matches any include pattern.

    A pattern ending in ``/`` (or naming a directory) matches everything under
    it: ``output/`` matches ``output/data.jsonl``. Otherwise shell-style globs
    apply to both the basename and the full relative path so ``*.log`` and
    ``logs/run.log`` both work.
    """
    norm = rel.replace(os.sep, "/")
    base = norm.rsplit("/", 1)[-1]
    for raw in patterns:
        pat = raw.replace(os.sep, "/")
        if pat.endswith("/"):
            prefix = pat.rstrip("/")
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
            continue
        # bare directory name (no glob, no slash) -> treat as a subtree prefix
        # ONLY. Without the ``continue`` this also fell through to the basename
        # fnmatch below, so ``output`` spuriously matched a file ``data/output``
        # nested anywhere (over-capture).
        if ("*" not in pat and "?" not in pat and "[" not in pat
                and "/" not in pat):
            if norm == pat or norm.startswith(pat + "/"):
                return True
            continue
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(norm, pat):
            return True
    return False


def list_ignored_files(repo: str) -> List[str]:
    """Untracked files that ARE under .gitignore (the opt-in candidate set).

    ``--others --ignored --exclude-standard`` lists exactly the ignored
    untracked paths (e.g. build artifacts under ``output/``).
    """
    out = run_git(
        repo,
        ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"],
    ).stdout
    return _decode_z(out)


def run_git_to_file(
    repo: str,
    args: List[str],
    out_fh,
    extra_env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run git streaming stdout straight to an open file handle (no buffering).

    Used by restore to pipe ``cat-file blob`` into the target file with RSS
    O(64 KiB) even for multi-GiB blobs, instead of holding the whole
    blob in a Python bytes object. ``timeout`` defaults to the generous
    :data:`RESTORE_TIMEOUT` (not the tight capture ceiling) so a large blob on a
    slow mount is not truncated; on expiry the caller's per-file isolation skips
    that one file.
    """
    env = _child_env(extra_env)
    return subprocess.run(
        ["git", "-C", repo] + _GLOBAL_CONFIG + args,
        env=env,
        stdout=out_fh,
        stderr=subprocess.PIPE,
        check=False,
        timeout=RESTORE_TIMEOUT if timeout is None else timeout,
    )


def missing_objects(repo: str, shas) -> "set":
    """Return the subset of object ids whose objects are absent from the DB.

    One ``cat-file --batch-check`` fork over all candidates: a record of the form
    ``<id> missing`` marks an absent object (a present one reads ``<id> <type>
    <size>``). Used by capture to demote a cached blob SHA whose object was
    pruned / GC'd back to a cold re-hash, instead of feeding a dead SHA into
    ``write-tree`` (which fails hard and would otherwise leave the poisoned cache
    in place, silently killing every subsequent protective snapshot).
    """
    ids = [s for s in shas if s]
    if not ids:
        return set()
    payload = ("\n".join(ids) + "\n").encode("ascii")
    proc = run_git(
        repo, ["cat-file", "--batch-check"], input_bytes=payload, check=False
    )
    missing = set()
    for line in proc.stdout.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "missing":
            missing.add(parts[0])
    return missing


def list_deleted_cached(repo: str) -> List[str]:
    """Tracked files deleted from the work tree but still in the index.

    These are exactly the paths ``--cached`` still lists yet ``os.lstat`` cannot
    stat. Their pre-deletion content lives in the object DB (staged blob), so the
    snapshot can preserve it without re-hashing.
    """
    out = run_git(repo, ["ls-files", "-z", "--deleted"]).stdout
    return _decode_z(out)


def list_tracked(repo: str) -> List[str]:
    """Paths tracked in the index (stage 0), repo-relative.

    Used to exempt already-committed/staged files from the secret floor:
    their blobs are already in the object store, so snapshotting
    them leaks nothing, while excluding them would leave a tracked
    ``id_rsa`` / ``server.pem`` / ``credentials.json`` unprotected. ``-z`` keeps
    odd names intact; surrogateescape so a non-UTF-8 tracked name never raises.
    """
    out = run_git(repo, ["ls-files", "-z"]).stdout
    return _decode_z(out)


def cached_stage_info(repo: str) -> Dict[str, Tuple[str, str]]:
    """``{path: (mode, blob_sha)}`` for stage-0 index entries.

    Source of the cached ``(mode, sha)`` injected for tracked-deleted files
    so ``write-tree`` includes the staged blob with zero re-hash.
    """
    out = run_git(repo, ["ls-files", "-s", "-z"]).stdout
    info: Dict[str, Tuple[str, str]] = {}
    for chunk in out.split(b"\x00"):
        if not chunk:
            continue
        meta, _, path = chunk.partition(b"\t")
        parts = meta.split()
        # format: <mode> SP <sha> SP <stage>
        if len(parts) >= 3 and parts[2] == b"0":
            info[path.decode("utf-8", "surrogateescape")] = (
                parts[0].decode("ascii"),
                parts[1].decode("ascii"),
            )
    return info


def list_worktree_files(
    repo: str,
    include_ignored: Optional[List[str]] = None,
    secret_patterns: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Files to snapshot, plus any secret paths that were excluded.

    Stage 1 (always): tracked + untracked, ``.gitignore``'d ones EXCLUDED
    (``--cached --others --exclude-standard``). This is the default and
    matches the secret-safe posture.

    Stage 2 (opt-in): when ``include_ignored`` patterns are given, also pull in
    ignored untracked paths that match those patterns (e.g. ``output/``).

    Stage 3 (always, even with opt-in): every UNTRACKED candidate path is run
    through the secret matcher; matches are dropped and returned separately so the
    caller can report them. Files tracked in the index are EXEMPT from the floor:
    their blobs are already in the object store, so snapshotting them
    leaks nothing, and excluding them would leave a tracked ``id_rsa`` /
    ``server.pem`` / ``credentials.json`` unprotected for zero secrecy benefit. So
    the floor only ever keeps an UNTRACKED secret out of the snapshot.

    ``-z`` keeps odd names (spaces / newlines / non-ASCII) intact. We dedupe
    because cached and others can overlap in rare edge cases.

    Returns ``(files, dropped_secrets)``.
    """
    out = run_git(
        repo,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    ).stdout
    seen: List[str] = []
    seen_set = set()
    for path in _decode_z(out):
        if _is_snap_internal(path):
            continue  # never re-capture our own .snap-bak
        if path not in seen_set:
            seen_set.add(path)
            seen.append(path)

    if include_ignored:
        for path in list_ignored_files(repo):
            if path in seen_set or _is_snap_internal(path):
                continue
            if _matches_any(path, include_ignored):
                seen_set.add(path)
                seen.append(path)

    # Stage 3: strip secrets from UNTRACKED paths only (tracked files are exempt;
    # see docstring). One extra ``ls-files`` fork buys the tracked set.
    tracked = set(list_tracked(repo))
    kept: List[str] = []
    dropped: List[str] = []
    for rel in seen:
        if rel in tracked or not secret.is_secret(rel, secret_patterns):
            kept.append(rel)
        else:
            dropped.append(rel)
    return kept, dropped
