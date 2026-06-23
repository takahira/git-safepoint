#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retention / GC: generation cap, size cap, dry-run, object reclaim."""
import os
import subprocess
import unittest

from tests import helpers
from git_safepoint import engine

import argparse
import time

from git_safepoint import gitutil, ids, cli
from git_safepoint.lock import LockBusy, RepoLock


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _make_n_snapshots(self, n):
        for i in range(n):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            res = engine.capture(self.repo, via="manual", debounce=0, force=True)
            self.assertEqual(res.status, 0, res.reason)

    def test_keep_generations_caps_count(self):
        self._make_n_snapshots(5)
        self.assertEqual(len(engine.list_refs(self.repo)), 5)
        summary = engine.prune(
            self.repo, keep_generations=3, max_bytes=-1, keep_hours=-1
        )
        self.assertEqual(summary["pruned"], 2)
        self.assertEqual(len(engine.list_refs(self.repo)), 3)

    def test_dry_run_is_noop(self):
        self._make_n_snapshots(5)
        summary = engine.prune(
            self.repo, keep_generations=2, max_bytes=-1, keep_hours=-1,
            dry_run=True,
        )
        self.assertEqual(summary["pruned"], 3)
        self.assertTrue(summary["dry_run"])
        # Nothing actually removed.
        self.assertEqual(len(engine.list_refs(self.repo)), 5)

    def test_max_bytes_caps_size(self):
        # Each snapshot holds a sizeable file; cap to keep only the newest few.
        for i in range(4):
            helpers.write_file(self.repo, "big.txt", "X" * 1000 + str(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        # cap below 2x file size so only the newest snapshot(s) survive.
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=1500, keep_hours=-1
        )
        self.assertGreaterEqual(summary["pruned"], 1)
        self.assertLess(len(engine.list_refs(self.repo)), 4)

    def test_max_bytes_never_drops_newest(self):
        # Regression: a max_bytes smaller than a single snapshot must NOT drop
        # the newest snapshot too. The safety net's last capture is always kept.
        for i in range(2):
            helpers.write_file(self.repo, "big.txt", "X" * 1000 + str(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(len(engine.list_refs(self.repo)), 2)
        # Cap is below even one snapshot's size.
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=100, keep_hours=-1
        )
        # Older one is pruned, but the newest survives (kept >= 1, refs >= 1).
        self.assertGreaterEqual(summary["kept"], 1)
        remaining = engine.list_refs(self.repo)
        self.assertEqual(len(remaining), 1)
        # The surviving ref is the newest (index 0 of the original list order).
        self.assertEqual(summary["pruned"], 1)

    def test_max_bytes_zero_keeps_only_newest(self):
        # Even max_bytes=0 (everything "exceeds" the cap) keeps exactly one ref.
        for i in range(3):
            helpers.write_file(self.repo, "big.txt", "Y" * 500 + str(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=0, keep_hours=-1
        )
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        self.assertEqual(summary["kept"], 1)

    def test_keep_hours_drops_old(self):
        # Plant a realistic ancient snapshot: BOTH an ancient timestamp id AND an
        # ancient committer date (list_refs now orders by committerdate, so the
        # planted ref must carry a matching old date to sort as oldest, not just
        # an old id string). Then a fresh one.
        helpers.write_file(self.repo, "f.txt", "now")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        empty_tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        old_date = "1999-01-01T00:00:00"
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = old_date
        env["GIT_AUTHOR_DATE"] = old_date
        commit = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", empty_tree, "-m", "old"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        old_id = "19990101-000000-0001-1"
        helpers.git(
            self.repo, "update-ref", engine.SNAP_REF_PREFIX + old_id, commit
        )
        self.assertEqual(len(engine.list_refs(self.repo)), 2)
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=-1, keep_hours=72
        )
        self.assertEqual(summary["pruned"], 1)
        remaining = {sid for sid, _ in engine.list_refs(self.repo)}
        self.assertNotIn(old_id, remaining)

    def test_gc_reclaims_after_prune(self):
        # Make snapshots with unique content so objects are distinct, then prune
        # and gc. Assert the refs drop AND the loose-object count falls. gc now
        # uses git's DEFAULT prune grace (never --prune=now), so the
        # loose count drops because the objects are PACKED (and possibly pruned
        # on the normal schedule); either way "loose objects packed/pruned" > 0.
        for i in range(6):
            helpers.write_file(self.repo, "f.txt", "unique-content-{0}".format(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        before = engine._loose_object_count(self.repo)
        self.assertGreater(before, 0)
        # keep newest 2 by generation; keep_hours=-1 disables the time policy.
        summary = engine.prune(
            self.repo, keep_generations=2, max_bytes=-1, keep_hours=-1,
            run_gc=True,
        )
        self.assertEqual(len(engine.list_refs(self.repo)), 2)
        after = engine._loose_object_count(self.repo)
        # gc packs the loose objects, so the loose count must fall.
        self.assertGreater(summary["reclaimed_loose"], 0)
        self.assertEqual(summary["reclaimed_loose"], before - after)
        self.assertLess(after, before)

    def test_no_gc_drops_refs_only(self):
        self._make_n_snapshots(4)
        summary = engine.prune(
            self.repo, keep_generations=1, max_bytes=-1, keep_hours=-1,
            run_gc=False,
        )
        self.assertEqual(summary["pruned"], 3)
        self.assertEqual(summary["reclaimed_loose"], 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)


class PruneSeqNewestKeptTest(unittest.TestCase):
    """Prune never drops the genuinely-newest (highest-seq) snapshot,
    even when a backward clock jump gave it an earlier committerdate so it sorts
    below an older snapshot.

    capture() now stamps wall-clock now and scrubs any leaked GIT_*_DATE,
    so the clock anomaly is manufactured by RE-STAMPING the older snapshot's
    commit with a far-future committerdate via a raw commit-tree (which bypasses
    the scrub), rather than by letting capture honour an injected date."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _restamp(self, ref, date):
        # Rebuild ref's commit over the SAME tree with a forced committerdate, via
        # raw git (not gitutil, which now scrubs GIT_*_DATE). The id/seq encoded in
        # the ref NAME is unchanged; only the commit's date moves.
        tree = helpers.git(
            self.repo, "rev-parse", "{0}^{{tree}}".format(ref)
        ).stdout.decode().strip()
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
        commit = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-m", "restamp"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        helpers.git(self.repo, "update-ref", ref, commit)

    def test_seq_newest_survives_prune_when_committerdate_is_older(self):
        # Capture A (lower seq) then B, the genuinely-newest capture (higher seq).
        helpers.write_file(self.repo, "f.txt", "v1")
        a = engine.capture(self.repo, via="manual", force=True, debounce=0)
        self.assertEqual(a.status, 0, a.reason)
        helpers.write_file(self.repo, "f.txt", "v2")
        b = engine.capture(self.repo, via="manual", force=True, debounce=0)
        self.assertEqual(b.status, 0, b.reason)

        # Clock anomaly: re-stamp A with a FAR-FUTURE date so it sorts as the
        # committerdate-newest (idx 0) while B (higher seq) sinks below it.
        self._restamp(engine.resolve_ref(a.id), "@4102444800 +0000")  # 2100-01-01

        rows = engine._refs_with_dates(self.repo)
        self.assertEqual(rows[0][2], a.id, "A should sort as committerdate-newest")
        self.assertEqual(rows[1][2], b.id, "B should have sunk below A by date")

        # Prune to a single generation: idx>=1 is dropped by the generation policy,
        # so without the seq-newest guard B (at idx 1) would be lost.
        engine.prune(
            self.repo, keep_generations=1, max_bytes=-1, keep_hours=-1, run_gc=False
        )
        ids_after = [sid for sid, _ref in engine.list_refs(self.repo)]
        self.assertIn(b.id, ids_after, "genuinely-newest (seq-newest) was pruned")


class PruneLockBusyExitTest(unittest.TestCase):
    """N5: prune exits non-zero (not a misleading success) when the lock is busy."""

    def test_lock_busy_returns_nonzero(self):
        from git_safepoint.lock import RepoLock
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "x")
            engine.capture(repo, via="manual", force=True, debounce=0)
            holder = RepoLock(os.path.join(gitutil.snap_dir(repo), "lock"))
            holder.acquire()
            try:
                args = argparse.Namespace(
                    keep_generations=50, max_bytes=-1, keep_hours=-1,
                    dry_run=False, no_gc=True,
                )
                rc = cli.cmd_prune(repo, args)
            finally:
                holder.release()
            self.assertEqual(rc, 1)
        finally:
            helpers.cleanup(repo)


class PruneGcGracePinnedTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_user_dangling_object_survives_aggressive_gc_config(self):
        # User configures an aggressive prune grace (a common tidy-repo setting).
        helpers.git(self.repo, "config", "gc.pruneExpire", "now")
        helpers.git(self.repo, "config", "gc.reflogExpireUnreachable", "now")
        # A real commit, then a dangling commit (as left by reset --hard / stash drop).
        helpers.write_file(self.repo, "real.txt", "base")
        helpers.git(self.repo, "add", "real.txt")
        helpers.git(self.repo, "commit", "-qm", "base")
        tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        head = helpers.git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        dangling = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-p", head,
             "-m", "dangling user work"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        self.assertEqual(
            helpers.git(self.repo, "cat-file", "-e", dangling,
                        check=False).returncode, 0)

        # Several snapshots so prune actually drops refs and runs gc.
        for i in range(4):
            helpers.write_file(self.repo, "f.txt", "snap{0}".format(i))
            engine.capture(self.repo, force=True, debounce=0)
        summary = engine.prune(
            self.repo, keep_generations=1, max_bytes=-1, keep_hours=-1,
            run_gc=True,
        )
        self.assertGreater(summary["pruned"], 0)
        self.assertEqual(
            helpers.git(self.repo, "cat-file", "-e", dangling,
                        check=False).returncode, 0,
            "prune's gc must not force-prune the user's own unreachable objects",
        )


# --- lock + seq are shared across linked worktrees ---------------


class C2PruneGcLockTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_gc_runs_without_holding_the_shared_lock(self):
        helpers.write_file(self.repo, "a.txt", "v1")
        engine.capture(self.repo, via="manual", force=True, debounce=0)
        helpers.write_file(self.repo, "a.txt", "v2")
        engine.capture(self.repo, via="manual", force=True, debounce=0)

        lock_path = os.path.join(gitutil.shared_snap_dir(self.repo), "lock")
        observed = {}
        orig = engine._run_gc_reclaim

        def spy(repo):
            # gc must run with the lock RELEASED so a protective capture the
            # instant before a destructive command is never blocked (C2).
            probe = RepoLock(lock_path)
            try:
                probe.acquire()
                observed["free"] = True
                probe.release()
            except LockBusy:
                observed["free"] = False
            return orig(repo)

        engine._run_gc_reclaim = spy
        try:
            summary = engine.prune(
                self.repo, keep_generations=1, keep_hours=-1, max_bytes=-1
            )
        finally:
            engine._run_gc_reclaim = orig
        self.assertGreaterEqual(summary["pruned"], 1, "a snapshot must be pruned")
        self.assertTrue(observed.get("free"), "lock must be free while gc runs")


# --------------------------------------------------------------------------- C3


class PruneFutureRefTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _plant_future_ref(self):
        future = int(time.time()) + 400 * 24 * 3600
        snap_id = ids.make_id(1, epoch=future)  # low seq -> not seq-newest
        tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = "@{0} +0000".format(future)
        env["GIT_AUTHOR_DATE"] = "@{0} +0000".format(future)
        commit = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-m", "future"],
            env=env, stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        ref = engine.SNAP_REF_PREFIX + snap_id
        helpers.git(self.repo, "update-ref", ref, commit)
        return ref

    def test_future_stamped_ref_is_pruned_first_not_protected(self):
        for i in range(3):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            engine.capture(self.repo, via="manual", force=True, debounce=0)
        genuine_newest = engine.list_refs(self.repo)[0][1]  # full ref
        future_ref = self._plant_future_ref()
        # Keep newest 2: pre-fix the future ref sorts as "newest" and survives
        # while genuine refs are evicted; the demotion makes it prune FIRST.
        engine.prune(
            self.repo, keep_generations=2, keep_hours=-1, max_bytes=-1,
            run_gc=False,
        )
        remaining = {ref for _id, ref in engine.list_refs(self.repo)}
        self.assertNotIn(
            future_ref, remaining,
            "future-stamped ref must be demoted and pruned first",
        )
        self.assertIn(
            genuine_newest, remaining, "genuine newest must be kept",
        )


# ------------------------------------------------------------- cli lows / nits


class PruneFutureRefAgeTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _plant_future_ref(self):
        future = int(__import__("time").time()) + 400 * 24 * 3600
        snap_id = ids.make_id(1, epoch=future)  # low seq -> not seq-newest
        tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = "@{0} +0000".format(future)
        env["GIT_AUTHOR_DATE"] = "@{0} +0000".format(future)
        import subprocess
        commit = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-m", "future"],
            env=env, stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        ref = engine.SNAP_REF_PREFIX + snap_id
        helpers.git(self.repo, "update-ref", ref, commit)
        return ref

    def test_future_ref_pruned_under_age_only_policy(self):
        for i in range(2):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            engine.capture(self.repo, via="manual", force=True, debounce=0)
        genuine_newest = engine.list_refs(self.repo)[0][1]
        future_ref = self._plant_future_ref()
        # AGE-ONLY policy (keep_generations=-1): genuine refs are recent -> kept;
        # the future ref's cdate > cutoff so pre-fix it survived forever. The D fix
        # drops a clearly-future cdate under the age policy too.
        engine.prune(
            self.repo, keep_generations=-1, keep_hours=72, max_bytes=-1,
            run_gc=False,
        )
        remaining = {ref for _id, ref in engine.list_refs(self.repo)}
        self.assertNotIn(future_ref, remaining, "future ref must be age-pruned (D)")
        self.assertIn(genuine_newest, remaining)


# ------------------------------------------------------------------------- E


class PruneAlwaysKeepsNewest(unittest.TestCase):
    """H2: every retention policy must preserve the newest snapshot."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _plant(self, snap_id, date):
        tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
        commit = subprocess.run(
            ["git", "-C", self.repo, "commit-tree", tree, "-m", snap_id],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        helpers.git(
            self.repo, "update-ref", engine.SNAP_REF_PREFIX + snap_id, commit
        )

    def test_keep_hours_keeps_newest_when_all_old(self):
        self._plant("19990101-000000-0001-1", "1999-01-01T00:00:00")
        self._plant("20000101-000000-0002-1", "2000-01-01T00:00:00")
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=-1, keep_hours=72
        )
        self.assertEqual(summary["kept"], 1)
        remaining = {sid for sid, _ in engine.list_refs(self.repo)}
        self.assertEqual(remaining, {"20000101-000000-0002-1"})

    def test_keep_generations_zero_keeps_newest(self):
        for i in range(3):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        summary = engine.prune(
            self.repo, keep_generations=0, max_bytes=-1, keep_hours=-1
        )
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        self.assertEqual(summary["kept"], 1)


class PruneSizeDedup(unittest.TestCase):
    """M7: max_bytes must count a blob shared across snapshots only once."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_shared_blob_counted_once(self):
        helpers.write_file(self.repo, "big.bin", "X" * 4096)
        for i in range(3):
            helpers.write_file(self.repo, "marker.txt", "v{0}".format(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True)
        # Logical sum would be ~3*4096 = 12288; deduped it is ~4096 + 3 markers.
        # A cap of 5000 keeps all three with dedup, but would prune with the old
        # double-counting accounting.
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=5000, keep_hours=-1,
            dry_run=True, run_gc=False,
        )
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(summary["kept"], 3)


class PruneBudgetAccounting(unittest.TestCase):
    """R4 / Codex P2: the newest snapshot's bytes count toward max_bytes."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_newest_bytes_charged_to_budget(self):
        helpers.write_file(self.repo, "f.txt", "A" * 30)   # older unique blob
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        helpers.write_file(self.repo, "f.txt", "B" * 80)   # newest unique blob
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        # 80 (newest) + 30 (older) = 110 > 100 -> the older one must be dropped.
        summary = engine.prune(
            self.repo, keep_generations=-1, max_bytes=100, keep_hours=-1,
            dry_run=True, run_gc=False,
        )
        self.assertEqual(summary["pruned"], 1)
        self.assertEqual(summary["kept"], 1)

if __name__ == "__main__":
    unittest.main()
