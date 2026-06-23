#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ID minting + cross-process collision resistance."""
import os
import subprocess
import sys
import unittest

from tests import helpers
from git_safepoint import engine, ids
from git_safepoint.lock import RepoLock

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from unittest import mock


class IdFormatTest(unittest.TestCase):
    def test_make_id_shape(self):
        sid = ids.make_id(3, epoch=1780000000, pid=48211)
        parts = sid.split("-")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[2], "0003")
        self.assertEqual(parts[3], "48211")

    def test_epoch_round_trip(self):
        sid = ids.make_id(1, epoch=1780000000, pid=1)
        self.assertGreater(ids.epoch_from_id(sid), 0)

    def test_epoch_bad_id(self):
        self.assertEqual(ids.epoch_from_id("garbage"), 0)


class PersistentSeqTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        self.snap = os.path.join(self.repo, ".git", "snap")
        os.makedirs(self.snap, exist_ok=True)
        self.seq_path = os.path.join(self.snap, "seq")

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_seq_is_monotonic_in_process(self):
        # Same-process sanity: consecutive calls read-modify-write the file.
        a = ids.next_seq(self.seq_path)
        b = ids.next_seq(self.seq_path)
        c = ids.next_seq(self.seq_path)
        self.assertEqual([a, b, c], [1, 2, 3])

    def test_seq_is_persistent_across_real_subprocesses(self):
        # Real cross-process check: spawn separate Python interpreters that each
        # call next_seq() on the same seq file. The persisted counter must keep
        # advancing across genuine process boundaries (not a same-process
        # simulation), proving durability of .git/snap/seq.
        prog = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from git_safepoint import ids; "
            "print(ids.next_seq(sys.argv[2]))"
        )
        seen = []
        for _ in range(5):
            out = subprocess.run(
                [sys.executable, "-c", prog, PKG_ROOT, self.seq_path],
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.decode().strip()
            seen.append(int(out))
        self.assertEqual(seen, [1, 2, 3, 4, 5])
        # The on-disk value matches the last subprocess's value.
        self.assertEqual(ids._read_seq(self.seq_path), 5)


class RapidCaptureCollisionTest(unittest.TestCase):
    """Many captures within the same second must produce distinct refs."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_rapid_captures_distinct_refs(self):
        ids_seen = set()
        for i in range(12):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            res = engine.capture(self.repo, via="manual", debounce=0, force=True)
            self.assertEqual(res.status, 0, res.reason)
            self.assertNotIn(res.id, ids_seen)
            ids_seen.add(res.id)
        rows = engine.list_refs(self.repo)
        self.assertEqual(len(rows), 12)
        # All refs are unique.
        self.assertEqual(len({r for _, r in rows}), 12)

    def test_existing_ref_forces_retry(self):
        # Pre-create the ref that seq=1 would mint, so capture must retry.
        # Freeze the timestamp so both make_id(1) calls produce the IDENTICAL id,
        # forcing a real create-only collision. The old test relied on the wall
        # clock staying within one second between the two calls and passed
        # VACUOUSLY across a second boundary (different timestamp -> no collision,
        # yet still != sid1); freezing makes it deterministic.
        from unittest import mock
        from git_safepoint import ids as idsmod

        FIXED = 1780000000
        helpers.write_file(self.repo, "f.txt", "v")
        sid1 = idsmod.make_id(1, epoch=FIXED)  # pid defaults to os.getpid()
        # Plant a ref with that exact name pointing at an empty-tree commit.
        empty_tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        commit = helpers.git(
            self.repo, "commit-tree", empty_tree, "-m", "planted"
        ).stdout.decode().strip()
        helpers.git(
            self.repo, "update-ref", engine.SNAP_REF_PREFIX + sid1, commit
        )
        # Capture in the same process (same pid) with the frozen epoch, so its
        # internal make_id(1) reproduces sid1 exactly -> collision -> retry.
        real_make_id = idsmod.make_id

        def frozen(seq, epoch=None, pid=None):
            return real_make_id(seq, epoch=FIXED, pid=pid)

        with mock.patch.object(engine.ids, "make_id", side_effect=frozen):
            res = engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(res.status, 0)
        self.assertEqual(
            engine._id_seq(res.id), 2,
            "create-only collision on seq=1 must advance the seq to 2",
        )
        self.assertNotEqual(res.id, sid1)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        self.snap = os.path.join(self.repo, ".git", "snap")
        os.makedirs(self.snap, exist_ok=True)
        self.lock_path = os.path.join(self.snap, "lock")

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_busy_lock_skips_capture(self):
        helpers.write_file(self.repo, "f.txt", "x")
        held = RepoLock(self.lock_path)
        held.acquire()
        try:
            res = engine.capture(self.repo, via="manual", debounce=0)
            # Lock busy -> skip with status 1 (fail-open, no block).
            self.assertEqual(res.status, 1)
        finally:
            held.release()
        # After release, capture works.
        res2 = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(res2.status, 0)


class IdRoundTrip(unittest.TestCase):
    """L11: snapshot id timestamp round-trips exactly in UTC."""

    def test_epoch_roundtrip_is_exact(self):
        for epoch in (1762065000, 1, 1893456000):
            sid = ids.make_id(1, epoch=epoch, pid=1)
            self.assertEqual(ids.epoch_from_id(sid), epoch)


# ---------------------------------------------------------------------------
# Second-pass (re-review) regressions introduced by the first fix round.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
