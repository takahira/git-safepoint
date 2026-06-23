#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Debounce change-detection dimensions.

The debounce decision compares the signature SET (path -> [mtime_ns, size,
inode]) of the work tree against the last snapshot's. Already proven elsewhere:

- ``test_incremental.test_debounce_skips_unchanged`` -- no change -> skip.
- ``test_incremental.test_debounce_captures_on_change`` -- content edit -> capture.
- ``test_hook_e2e.test_hook_double_fire_debounced`` -- hook double-fire -> skip.

What those leave open is the *structural* dimensions: a file ADDED or REMOVED
within the debounce window changes the signature SET (not any single file's
content) and must still defeat the debounce. This module closes that gap so the
"変化判定" claim is backed end to end, not only for in-place content edits.
"""
import os
import unittest

from tests import helpers
from git_safepoint import engine


class DebounceChangeDetectionTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        helpers.write_file(self.repo, "a.txt", "alpha")
        helpers.write_file(self.repo, "b.txt", "bravo")

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _capture(self):
        # debounce active (5s window): the only thing that lets a re-capture
        # through is a genuine change in the signature set.
        return engine.capture(self.repo, via="manual", debounce=5)

    def test_added_file_defeats_debounce(self):
        self.assertEqual(self._capture().status, 0)
        # A brand-new untracked file appears within the window -> the signature
        # set gains a key -> cur != prev -> capture (not debounced).
        helpers.write_file(self.repo, "c.txt", "charlie")
        r = self._capture()
        self.assertEqual(r.status, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 2)

    def test_removed_file_defeats_debounce(self):
        self.assertEqual(self._capture().status, 0)
        # Deleting a file drops a key from the current signature set, so it
        # differs from the cached set -> capture (not debounced).
        os.unlink(os.path.join(self.repo, "b.txt"))
        r = self._capture()
        self.assertEqual(r.status, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 2)

    def test_force_overrides_debounce_when_unchanged(self):
        self.assertEqual(self._capture().status, 0)
        # Nothing changed, but force bypasses the debounce gate entirely.
        r = engine.capture(self.repo, via="manual", debounce=5, force=True)
        self.assertEqual(r.status, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 2)


if __name__ == "__main__":
    unittest.main()
