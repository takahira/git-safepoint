#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch hashing: many files in one git fork per chunk, with the per-file
safety net preserved (odd names, chunk boundaries, bad-file isolation, mixed
symlinks). Locks in the cold-capture optimisation."""
import os
import unittest

from tests import helpers
from git_safepoint import engine


class BatchHashTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        # Small chunk size so a handful of files crosses several chunk
        # boundaries -- exercises the chunk loop and per-chunk fallback without
        # creating hundreds of files.
        self._orig_batch = engine.HASH_BATCH_MAX
        engine.HASH_BATCH_MAX = 3

    def tearDown(self):
        engine.HASH_BATCH_MAX = self._orig_batch
        helpers.cleanup(self.repo)

    def _names_in(self, snap_id):
        ref = engine.resolve_ref(snap_id)
        out = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", "-z", ref
        ).stdout
        return [c.decode("utf-8", "surrogateescape") for c in out.split(b"\x00") if c]

    def test_many_files_cross_chunk_boundaries(self):
        # 10 files over a chunk size of 3 -> 4 chunks. All captured, content
        # faithful, order irrelevant.
        for i in range(10):
            helpers.write_file(self.repo, "d%d/f%d.txt" % (i % 3, i), "body-%d" % i)
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        self.assertEqual(res.files, 10)
        names = self._names_in(res.id)
        self.assertEqual(len(names), 10)
        # Spot-check faithful content via restore.
        os.unlink(os.path.join(self.repo, "d0/f0.txt"))
        engine.restore_file(self.repo, res.id, "d0/f0.txt")
        self.assertEqual(helpers.read_file(self.repo, "d0/f0.txt"), b"body-0")

    def test_newline_in_name_batches_correctly(self):
        # The exact case `git hash-object --stdin-paths` would corrupt; argv +
        # `update-index -z` keep it intact. Pad with siblings to force batching.
        for i in range(4):
            helpers.write_file(self.repo, "pad%d.txt" % i, str(i))
        nl = "weird\nname.txt"
        helpers.write_file(self.repo, nl, "newline-body")
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        self.assertIn(nl, self._names_in(res.id))
        os.unlink(os.path.join(self.repo, nl))
        status, _ = engine.restore_file(self.repo, res.id, nl)
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, nl), b"newline-body")

    def test_bad_file_isolated_within_large_batch(self):
        # An unreadable file in the middle of several chunks: it is skipped, the
        # batch falls back to per-file for its chunk, every other file survives.
        for i in range(9):
            helpers.write_file(self.repo, "f%d.txt" % i, "ok-%d" % i)
        bad = os.path.join(self.repo, "f4.txt")
        os.chmod(bad, 0o000)
        try:
            res = engine.capture(self.repo, via="manual")
            self.assertEqual(res.status, 0)
            names = self._names_in(res.id)
            self.assertEqual(len(names), 8, "exactly the one bad file is skipped")
            self.assertNotIn("f4.txt", names)
            self.assertIn("f0.txt", names)
            self.assertIn("f8.txt", names)
        finally:
            os.chmod(bad, 0o644)

    def test_symlinks_mixed_with_regular_files(self):
        # Symlinks must be hashed individually (never followed) even when many
        # regular files are batched alongside them.
        for i in range(5):
            helpers.write_file(self.repo, "r%d.txt" % i, "reg-%d" % i)
        os.symlink("r0.txt", os.path.join(self.repo, "good.lnk"))
        os.symlink("nowhere", os.path.join(self.repo, "dangling.lnk"))
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        self.assertEqual(res.files, 7)
        # The link blob holds the target string, not the target's bytes.
        ref = engine.resolve_ref(res.id)
        blob = helpers.git(self.repo, "cat-file", "blob", ref + ":good.lnk").stdout
        self.assertEqual(blob, b"r0.txt")
        os.unlink(os.path.join(self.repo, "good.lnk"))
        engine.restore_file(self.repo, res.id, "good.lnk")
        self.assertTrue(os.path.islink(os.path.join(self.repo, "good.lnk")))


if __name__ == "__main__":
    unittest.main()
