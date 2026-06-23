#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real-process concurrency: the lock *serialises* concurrent captures.

The protective capture path (hook / preexec / default manual) waits for the repo
lock rather than skipping on contention: the lock holder may be a restore / prune
/ a stale capture that does NOT cover the current work tree, so silently riding
along could lose disk-only unsaved work the instant before a destructive command
. This module exercises the model with genuine OS processes
(``multiprocessing`` with the ``spawn`` start method -- separate interpreters, no
shared in-process state), not a same-process simulation, and asserts that:

* every process terminates with status 0 (captured) or 1 (skipped) -- never a
  crash / exception / corrupted result;
* concurrent forced captures serialise behind the lock (all make progress)
  rather than all-but-one being dropped;
* the number of ``refs/snapshots/*`` equals the number of winners (no orphan or
  duplicate refs);
* all minted IDs are distinct (the persistent ``seq`` counter never repeats);
* ``git fsck --full`` reports no corruption (the object store / ref store are
  intact after the concurrent writers).

Two focused tests pin the review-B contract directly: a protective capture whose
lock is briefly held *waits and then captures* (does not skip), while a manual
``--no-wait`` capture skips immediately.

Gated: requires ``fcntl`` (the advisory lock degrades to a no-op without it) and
a working ``spawn`` multiprocessing context. Both are present on macOS / Linux;
the suite skips elsewhere.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import unittest

from tests import helpers

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import fcntl  # noqa: F401  (probe only)

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

try:
    _SPAWN_CTX = multiprocessing.get_context("spawn")
    _HAVE_SPAWN = True
except (ValueError, RuntimeError):  # pragma: no cover - exotic platforms
    _SPAWN_CTX = None
    _HAVE_SPAWN = False

import errno
import shutil
import tempfile
import threading
import time
from unittest import mock

from git_safepoint import engine, gitutil, ids, lock
from git_safepoint import lock as lockmod
from git_safepoint.lock import RepoLock


def _capture_worker(repo, barrier, result_q):
    """Run in a *separate* spawned interpreter: capture once, report (status, id).

    ``sys.path`` is not inherited reliably across the spawn boundary, so re-add
    the package root before importing the engine. The barrier makes all workers
    attempt the lock within the same instant to force genuine contention.
    """
    sys.path.insert(0, PKG_ROOT)
    from git_safepoint import engine

    try:
        barrier.wait()
    except Exception:  # pragma: no cover - barrier broken; still try to capture
        pass
    try:
        res = engine.capture(repo, via="manual", debounce=0, force=True)
        result_q.put(("ok", res.status, res.id))
    except Exception as exc:  # noqa: BLE001 - report, do not crash silently
        result_q.put(("exc", repr(exc), None))


@unittest.skipUnless(_HAVE_FCNTL, "fcntl required for advisory lock (POSIX only)")
@unittest.skipUnless(_HAVE_SPAWN, "spawn multiprocessing context unavailable")
class RealProcessContentionTest(unittest.TestCase):
    """N genuine processes snapshot one repo at once; lock arbitrates them."""

    N = 8

    def setUp(self):
        self.repo = helpers.make_repo()
        # Seed a handful of files so each capture does real work (hash +
        # write-tree + commit-tree + update-ref) while contending for the lock.
        for i in range(60):
            helpers.write_file(self.repo, "src/f{0}.txt".format(i),
                               "content {0}\n".format(i))

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _fsck_clean(self):
        proc = helpers.git(self.repo, "fsck", "--full", check=False)
        err = proc.stderr.decode("utf-8", "replace")
        # An unborn-HEAD notice is expected (we never commit to HEAD) and is not
        # corruption; any real error/fatal/missing/dangling line is.
        bad = [
            ln
            for ln in err.splitlines()
            if ln.strip()
            and "unborn branch" not in ln
            and not ln.startswith("Checking")
        ]
        return proc.returncode == 0, bad

    def _run_contention(self, n):
        barrier = _SPAWN_CTX.Barrier(n)
        result_q = _SPAWN_CTX.Queue()
        procs = [
            _SPAWN_CTX.Process(
                target=_capture_worker, args=(self.repo, barrier, result_q)
            )
            for _ in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
        # No worker may hang; every one must have exited.
        for p in procs:
            self.assertFalse(p.is_alive(), "a capture worker hung")
        results = [result_q.get() for _ in range(n)]
        return results

    def test_concurrent_captures_serialize_no_corruption(self):
        results = self._run_contention(self.N)

        # No worker raised an unexpected exception.
        exc = [r for r in results if r[0] == "exc"]
        self.assertEqual(exc, [], "workers raised: {0}".format(exc))

        statuses = [r[1] for r in results]
        ids = [r[2] for r in results if r[1] == 0 and r[2]]

        # Every worker ends 0 (captured) or 1 (skipped) -- never a crash.
        for st in statuses:
            self.assertIn(st, (0, 1), "unexpected status {0}".format(st))

        winners = statuses.count(0)
        skips = statuses.count(1)

        # winners + skips must account for all N (no lost workers).
        self.assertEqual(winners + skips, self.N)

        # Contract: forced captures SERIALISE behind the blocking
        # lock instead of all-but-one being dropped. With ``force=True`` (no
        # tree-SHA dedup) every worker that acquires the lock mints its own ref,
        # so more than one makes progress -- the opposite of the old first-wins
        # ride-along-skip model. (60 tiny files capture in well under the lock
        # timeout, so on this workload all N normally win; assert >1 with margin
        # against a pathologically slow runner timing a late waiter out.)
        self.assertGreater(
            winners, 1, "captures did not serialise; got {0} winner(s)".format(
                winners
            )
        )

        # Minted IDs are all distinct (persistent seq never repeats).
        self.assertEqual(len(set(ids)), len(ids), "duplicate snapshot IDs")

        # Ref store matches winners exactly -- no orphan / duplicate refs.
        rows = []
        out = helpers.git(
            self.repo, "for-each-ref", "refs/snapshots/"
        ).stdout.decode("utf-8")
        rows = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(rows), winners, "ref count != winner count")

        # The object store and ref store survive concurrent writers intact.
        ok, bad = self._fsck_clean()
        self.assertTrue(ok, "git fsck failed: {0}".format(bad))
        self.assertEqual(bad, [], "git fsck reported corruption: {0}".format(bad))

    def test_sequential_after_contention_still_captures(self):
        """After the contended round, a lone capture still succeeds (lock freed).

        Proves the losing processes released nothing they did not hold and the
        winner released the lock cleanly -- the repo is not wedged.
        """
        self._run_contention(self.N)
        from git_safepoint import engine

        helpers.write_file(self.repo, "src/f0.txt", "changed-different-length\n")
        res = engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(res.status, 0, res.reason)
        ok, bad = self._fsck_clean()
        self.assertTrue(ok, "git fsck failed after follow-up: {0}".format(bad))

    def test_protective_capture_waits_for_held_lock(self):
        """Review B: a hook/preexec capture WAITS for a briefly-held lock then
        captures -- it must not silently skip the protective snapshot."""
        import threading
        import time

        from git_safepoint import engine, gitutil

        # Lock lives on the SHARED (common) git dir, not the per-worktree
        # snap dir -- pin the production location so the test is not a main-
        # worktree-only coincidence.
        lock_path = os.path.join(gitutil.shared_snap_dir(self.repo), "lock")
        holder = engine.RepoLock(lock_path)
        holder.acquire()
        released = []

        def _release_soon():
            time.sleep(0.5)
            holder.release()
            released.append(True)

        t = threading.Thread(target=_release_soon)
        t.start()
        # via="hook" => protective path => blocking acquire. Must wait out the
        # 0.5s hold (< the lock timeout) and capture, not skip.
        res = engine.capture(self.repo, via="hook", debounce=0)
        t.join()
        self.assertTrue(released, "lock was never released by the holder")
        self.assertEqual(
            res.status, 0,
            "protective capture should wait then capture, got: {0}".format(
                res.reason
            ),
        )

    def test_no_wait_skips_immediately_under_contention(self):
        """Manual --no-wait still skips at once when the lock is held."""
        from git_safepoint import engine, gitutil

        # Lock lives on the SHARED (common) git dir, not the per-worktree
        # snap dir -- pin the production location so the test is not a main-
        # worktree-only coincidence.
        lock_path = os.path.join(gitutil.shared_snap_dir(self.repo), "lock")
        holder = engine.RepoLock(lock_path)
        holder.acquire()
        try:
            res = engine.capture(
                self.repo, via="manual", no_wait=True, debounce=0, force=True
            )
            self.assertEqual(res.status, 1)
            self.assertIn("another snapshot in progress", res.reason)
        finally:
            holder.release()


class CaptureLockTimeoutSkipTest(unittest.TestCase):
    """N9: the protective capture skips (status 1) when the lock stays busy past
    the timeout -- the one deliberate data-loss window, now under test."""

    def test_protective_capture_skips_when_lock_held(self):
        from git_safepoint.lock import RepoLock
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "x")
            holder = RepoLock(os.path.join(gitutil.snap_dir(repo), "lock"))
            holder.acquire()
            saved = engine._CAPTURE_LOCK_TIMEOUT
            engine._CAPTURE_LOCK_TIMEOUT = 0.1  # keep the test fast
            try:
                res = engine.capture(repo, via="hook", conservative=True, debounce=0)
            finally:
                engine._CAPTURE_LOCK_TIMEOUT = saved
                holder.release()
            self.assertEqual(res.status, 1)
            self.assertIn("lock busy", res.reason)
        finally:
            helpers.cleanup(repo)


class FlockDegradeTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_flock_enolck_degrades_capture_restore_prune(self):
        if not lockmod._HAVE_FCNTL:
            self.skipTest("no fcntl on this platform")
        helpers.write_file(self.repo, "f.txt", "x")

        def boom(fd, op):
            raise OSError(errno.ENOLCK, "no locks available")

        # flock unsupported (lockd-less NFS etc.): every RepoLock.acquire must
        # degrade to a no-op, not re-raise. Pre-fix the hook capture silently
        # skipped and restore/prune crashed with a traceback.
        with mock.patch.object(lockmod.fcntl, "flock", side_effect=boom):
            res = engine.capture(self.repo, via="manual", force=True)
            self.assertEqual(res.status, 0, "capture must still fire (degraded lock)")
            st, msg = engine.restore_all(self.repo, res.id, no_backup=True)
            self.assertEqual(st, 0, msg)  # restore must not raise
            summary = engine.prune(
                self.repo, keep_generations=0, keep_hours=-1, max_bytes=-1,
                run_gc=False,
            )
            self.assertIn("pruned", summary)  # prune must not raise


# ------------------------------------------------------------------------- B


class CrossWorktreeConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_concurrent_captures_across_worktrees_stay_consistent(self):
        if not lockmod._HAVE_FCNTL:
            self.skipTest("no fcntl: lock is a no-op, contention test moot")
        # worktree add needs a HEAD commit.
        helpers.write_file(self.repo, "seed.txt", "seed")
        helpers.git(self.repo, "add", "seed.txt")
        helpers.git(self.repo, "commit", "-qm", "seed")
        wt = tempfile.mkdtemp(prefix="git-safepoint-wt2-")
        self.addCleanup(shutil.rmtree, wt, True)
        helpers.git(self.repo, "worktree", "add", "-q", wt, "-b", "wt2")

        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker(target, idx):
            helpers.write_file(target, "u{0}.txt".format(idx), "content-{0}".format(idx))
            barrier.wait()
            try:
                r = engine.capture(target, via="manual", force=True, debounce=0)
            except Exception as exc:  # noqa: BLE001
                r = exc
            with lock:
                results.append(r)

        threads = []
        for i in range(6):
            target = self.repo if i % 2 == 0 else wt
            t = threading.Thread(target=worker, args=(target, i))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # No capture raised; all distinct content -> all status 0 with distinct IDs.
        exes = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(exes, [], "no capture may raise under contention")
        ids_seen = [r.id for r in results if getattr(r, "status", 1) == 0]
        self.assertEqual(len(ids_seen), len(set(ids_seen)), "IDs must be unique (shared seq)")
        self.assertGreaterEqual(len(ids_seen), 6, "all captures succeed under blocking lock")
        # Ref store is intact.
        fsck = helpers.git(self.repo, "fsck", "--strict", check=False)
        self.assertEqual(fsck.returncode, 0, fsck.stderr.decode("utf-8", "replace"))

if __name__ == "__main__":
    unittest.main()
