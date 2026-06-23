#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git environment / config hardening (gitutil.py).

Pins the child-env scrub (GIT_CONFIG_* / date / author env), command-line
config-parameter scrubbing, subprocess timeouts, sidecar file permissions,
and shared-state isolation across linked worktrees.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import subprocess
import unittest
from contextlib import redirect_stderr

from tests import helpers
from git_safepoint import engine, gitutil


class GitConfigParametersScrubTest(unittest.TestCase):
    """GIT_CONFIG_PARAMETERS must not leak into git plumbing calls."""

    def setUp(self):
        self.repo = helpers.make_repo()
        self._saved = os.environ.pop("GIT_CONFIG_PARAMETERS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("GIT_CONFIG_PARAMETERS", None)
        else:
            os.environ["GIT_CONFIG_PARAMETERS"] = self._saved
        helpers.cleanup(self.repo)

    def test_scrubbed_from_child_env(self):
        os.environ["GIT_CONFIG_PARAMETERS"] = "'user.name=INJECTED'"
        self.assertNotIn("GIT_CONFIG_PARAMETERS", gitutil._child_env())

    def test_inherited_value_not_honoured_by_plumbing(self):
        # Without the scrub, git would apply the injected user.name to every call.
        os.environ["GIT_CONFIG_PARAMETERS"] = "'user.name=INJECTED'"
        self.assertEqual(gitutil.git_out(self.repo, ["config", "user.name"]), "test")


class DateEnvScrubbedTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        self._saved = {k: os.environ.get(k)
                       for k in ("GIT_COMMITTER_DATE", "GIT_AUTHOR_DATE")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        helpers.cleanup(self.repo)

    def test_leaked_committer_date_does_not_stamp_snapshot(self):
        os.environ["GIT_COMMITTER_DATE"] = "2001-01-01T00:00:00"
        os.environ["GIT_AUTHOR_DATE"] = "2001-01-01T00:00:00"
        helpers.write_file(self.repo, "f.txt", "x")
        r = engine.capture(self.repo, force=True)
        self.assertEqual(r.status, 0, r.reason)
        cdate = engine._refs_with_dates(self.repo)[0][0]
        self.assertGreater(
            cdate, 1_700_000_000,
            "snapshot committerdate should be ~now, not the leaked 2001 date",
        )

    def test_prune_keeps_fresh_snapshots_despite_leaked_old_date(self):
        os.environ["GIT_COMMITTER_DATE"] = "2001-01-01T00:00:00"
        os.environ["GIT_AUTHOR_DATE"] = "2001-01-01T00:00:00"
        for i in range(3):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            engine.capture(self.repo, force=True, debounce=0)
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=-1, keep_hours=72,
            run_gc=False,
        )
        self.assertEqual(summary["pruned"], 0,
                         "fresh snapshots must survive the 72h age policy")
        self.assertEqual(len(engine.list_refs(self.repo)), 3)


# --- cache self-heals when a reused blob was GC'd --------------------


class WorktreeSharedStateTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        helpers.write_file(self.repo, "a.txt", "x")
        helpers.git(self.repo, "add", "a.txt")
        helpers.git(self.repo, "commit", "-qm", "init")
        self.linked = self.repo + "-linked"
        helpers.git(self.repo, "worktree", "add", "-q", self.linked)

    def tearDown(self):
        helpers.git(self.repo, "worktree", "remove", "--force", self.linked,
                    check=False)
        shutil.rmtree(self.linked, ignore_errors=True)
        helpers.cleanup(self.repo)

    def test_lock_and_seq_shared_but_cache_per_worktree(self):
        # realpath so a /var -> /private/var (macOS) symlink prefix does not mask
        # that both resolve to the SAME physical dir (same inode -> flock holds).
        self.assertEqual(
            os.path.realpath(gitutil.shared_snap_dir(self.repo)),
            os.path.realpath(gitutil.shared_snap_dir(self.linked)),
            "lock+seq dir must be shared across worktrees",
        )
        self.assertNotEqual(
            os.path.realpath(gitutil.snap_dir(self.repo)),
            os.path.realpath(gitutil.snap_dir(self.linked)),
            "mtime-cache dir must stay per-worktree",
        )

    def test_seq_monotonic_and_refs_shared_across_worktrees(self):
        helpers.write_file(self.repo, "m.txt", "1")
        r1 = engine.capture(self.repo, force=True, debounce=0)
        helpers.write_file(self.linked, "l.txt", "1")
        r2 = engine.capture(self.linked, force=True, debounce=0)
        self.assertEqual(r1.status, 0, r1.reason)
        self.assertEqual(r2.status, 0, r2.reason)

        # refs/snapshots is shared, so both captures are visible from either side.
        ids_main = {sid for sid, _ in engine.list_refs(self.repo)}
        ids_linked = {sid for sid, _ in engine.list_refs(self.linked)}
        self.assertEqual(ids_main, ids_linked, "refs are shared across worktrees")
        self.assertIn(r1.id, ids_main)
        self.assertIn(r2.id, ids_main)
        self.assertNotEqual(r1.id, r2.id)

        # The shared seq is monotonic: the linked-worktree capture got the NEXT
        # seq, not a per-worktree reset back to 1 (which would collide).
        self.assertGreater(
            engine._id_seq(r2.id), engine._id_seq(r1.id),
            "seq must be monotonic across worktrees (shared counter)",
        )


# --- restore UX ------------------------------------------------


class H1HashIsolationTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_tracked_path_turned_fifo_does_not_abort_capture(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("platform has no mkfifo")
        # The exact H1 repro: a tracked file replaced by a FIFO. Pre-fix
        # ``git hash-object <fifo>`` blocked until GIT_TIMEOUT, raised
        # TimeoutExpired, and aborted the whole protective snapshot.
        helpers.write_file(self.repo, "data", "original")
        helpers.git(self.repo, "add", "data")
        helpers.git(self.repo, "commit", "-qm", "add data")
        os.unlink(os.path.join(self.repo, "data"))
        os.mkfifo(os.path.join(self.repo, "data"))
        helpers.write_file(self.repo, "other.txt", "keep me")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        ref = engine.resolve_ref(res.id)
        names = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", ref
        ).stdout.decode()
        self.assertIn("other.txt", names, "real file must still be captured")
        self.assertNotIn("data", names, "FIFO must be skipped, not hashed")
        self.assertIn("FIFO/socket/device", err.getvalue())

    def test_hash_many_blobs_isolates_timeout(self):
        # The backstop: a chunk hash-object that times out (and the per-file
        # retry too) must yield an empty result, never propagate TimeoutExpired.
        orig_run = gitutil.run_git
        orig_one = engine.hash_one_blob

        def boom_run(repo, args, **kw):
            if args[:1] == ["hash-object"]:
                raise subprocess.TimeoutExpired(cmd="git hash-object", timeout=1)
            return orig_run(repo, args, **kw)

        def boom_one(repo, path):
            raise subprocess.TimeoutExpired(cmd="git hash-object", timeout=1)

        gitutil.run_git = boom_run
        engine.hash_one_blob = boom_one
        try:
            out = engine.hash_many_blobs(self.repo, ["/x/a", "/x/b"])
        finally:
            gitutil.run_git = orig_run
            engine.hash_one_blob = orig_one
        self.assertEqual(out, {}, "timed-out paths omitted, no exception raised")


# --------------------------------------------------------------------------- H2


class GitEnvScrub(unittest.TestCase):
    """H1: inherited GIT_DIR/GIT_INDEX_FILE must not redirect git calls."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_inherited_git_env_does_not_redirect_capture(self):
        helpers.write_file(self.repo, "a.txt", "hello")
        helpers.write_file(self.repo, "b.txt", "world")
        other = helpers.make_repo()
        self.addCleanup(helpers.cleanup, other)
        saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_INDEX_FILE")}
        os.environ["GIT_DIR"] = os.path.join(other, ".git")
        os.environ["GIT_INDEX_FILE"] = os.path.join(other, ".git", "index")
        try:
            res = engine.capture(self.repo, via="manual", debounce=0, force=True)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(res.status, 0)
        self.assertEqual(res.files, 2)
        rows = engine.list_refs(self.repo)
        self.assertEqual(len(rows), 1)
        entries = engine._paths_under(self.repo, engine.resolve_ref(rows[0][0]), "")
        self.assertEqual({rel for _m, _s, rel in entries}, {"a.txt", "b.txt"})
        # The other repo must have received nothing.
        self.assertEqual(engine.list_refs(other), [])


@unittest.skipUnless(os.name == "posix", "POSIX file modes only")
class SidecarPermsTest(unittest.TestCase):
    """G: the snap sidecar state is owner-only regardless of umask."""

    def test_snap_dir_and_cache_and_seq_are_owner_only(self):
        old = os.umask(0o022)  # a permissive umask that would otherwise leak bits
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "x")
            engine.capture(repo, via="manual", force=True, debounce=0)
            snap = os.path.join(repo, ".git", "snap")
            self.assertEqual(stat.S_IMODE(os.stat(snap).st_mode) & 0o077, 0,
                             "snap dir leaks group/other bits")
            cache = os.path.join(snap, "mtime-cache.json")
            self.assertTrue(os.path.exists(cache))
            self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode) & 0o077, 0,
                             "mtime-cache.json leaks group/other bits")
            seq = os.path.join(snap, "seq")
            self.assertTrue(os.path.exists(seq))
            self.assertEqual(stat.S_IMODE(os.stat(seq).st_mode) & 0o077, 0,
                             "seq leaks group/other bits")
        finally:
            os.umask(old)
            helpers.cleanup(repo)


class GitTimeoutTest(unittest.TestCase):
    """L: git invocations honour a finite, env-overridable timeout."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_default_timeouts_are_finite_and_positive(self):
        self.assertGreater(gitutil.GIT_TIMEOUT, 0)
        self.assertGreater(gitutil.GC_TIMEOUT, gitutil.GIT_TIMEOUT)
        # Restore streams whole blobs -> a much more generous ceiling than the
        # tight capture timeout, so a large blob is not truncated.
        self.assertGreater(gitutil.RESTORE_TIMEOUT, gitutil.GIT_TIMEOUT)

    def test_run_git_raises_on_timeout(self):
        # A sub-millisecond timeout cannot outrun even git's process startup, so
        # subprocess kills the child and raises TimeoutExpired (pre-fix: no
        # timeout, so a genuinely hung git would block forever).
        with self.assertRaises(subprocess.TimeoutExpired):
            gitutil.run_git(self.repo, ["--version"], timeout=0.0001)

    def test_explicit_timeout_overrides_default(self):
        # A generous explicit timeout still completes normally.
        proc = gitutil.run_git(self.repo, ["--version"], timeout=30)
        self.assertEqual(proc.returncode, 0)

if __name__ == "__main__":
    unittest.main()
