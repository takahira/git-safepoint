#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture + restore core: untracked, symlink, dangling, gitignore, modes."""
import os
import unittest

from tests import helpers
from git_safepoint import engine

import contextlib
import io
import shutil
import stat
import tempfile
from contextlib import redirect_stderr
from unittest import mock

from git_safepoint import gitutil


class SnapshotCoreTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_captures_untracked_and_restores(self):
        helpers.write_file(self.repo, "notes/draft.md", "never committed")
        res = engine.capture(self.repo, label="t", via="manual")
        self.assertEqual(res.status, 0)
        self.assertEqual(res.files, 1)
        # Disaster: delete the untracked file.
        os.unlink(os.path.join(self.repo, "notes/draft.md"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "notes/draft.md")))
        # Restore.
        status, _ = engine.restore_file(self.repo, res.id, "notes/draft.md")
        self.assertEqual(status, 0)
        self.assertEqual(
            helpers.read_file(self.repo, "notes/draft.md"), b"never committed"
        )

    def test_index_and_head_untouched(self):
        helpers.write_file(self.repo, "tracked.txt", "x")
        helpers.git(self.repo, "add", "tracked.txt")
        helpers.git(self.repo, "commit", "-qm", "init")
        helpers.write_file(self.repo, "untracked.txt", "y")
        before_status = helpers.git(self.repo, "status", "--porcelain").stdout
        head_before = helpers.git(self.repo, "rev-parse", "HEAD").stdout
        engine.capture(self.repo, via="manual")
        after_status = helpers.git(self.repo, "status", "--porcelain").stdout
        head_after = helpers.git(self.repo, "rev-parse", "HEAD").stdout
        self.assertEqual(before_status, after_status)
        self.assertEqual(head_before, head_after)

    def test_gitignore_excluded(self):
        helpers.write_file(self.repo, ".gitignore", "ignore-me\n")
        helpers.write_file(self.repo, "ignore-me", "secret")
        helpers.write_file(self.repo, "keep.txt", "kept")
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        ref = engine.resolve_ref(res.id)
        names = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", ref
        ).stdout.decode()
        self.assertIn("keep.txt", names)
        self.assertNotIn("ignore-me", names)

    def test_symlink_round_trips_as_symlink(self):
        helpers.write_file(self.repo, "target.txt", "real")
        link = os.path.join(self.repo, "link")
        os.symlink("target.txt", link)
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        os.unlink(link)
        status, _ = engine.restore_file(self.repo, res.id, "link")
        self.assertEqual(status, 0)
        self.assertTrue(os.path.islink(os.path.join(self.repo, "link")))
        self.assertEqual(os.readlink(os.path.join(self.repo, "link")), "target.txt")

    def test_dangling_symlink_does_not_crash(self):
        link = os.path.join(self.repo, "dangling")
        os.symlink("nowhere-here", link)
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        os.unlink(link)
        status, _ = engine.restore_file(self.repo, res.id, "dangling")
        self.assertEqual(status, 0)
        self.assertTrue(os.path.islink(os.path.join(self.repo, "dangling")))
        self.assertEqual(
            os.readlink(os.path.join(self.repo, "dangling")), "nowhere-here"
        )

    def test_executable_bit_preserved(self):
        helpers.write_file(self.repo, "run.sh", "#!/bin/sh\necho hi\n", executable=True)
        res = engine.capture(self.repo, via="manual")
        os.unlink(os.path.join(self.repo, "run.sh"))
        engine.restore_file(self.repo, res.id, "run.sh")
        st = os.stat(os.path.join(self.repo, "run.sh"))
        self.assertTrue(st.st_mode & 0o100, "exec bit should be restored")

    def test_unreadable_file_skipped_not_fatal(self):
        helpers.write_file(self.repo, "good.txt", "ok")
        bad = helpers.write_file(self.repo, "bad.txt", "secret")
        os.chmod(bad, 0o000)
        try:
            res = engine.capture(self.repo, via="manual")
            # At least the good file is captured; snapshot succeeds.
            self.assertEqual(res.status, 0)
            self.assertGreaterEqual(res.files, 1)
            ref = engine.resolve_ref(res.id)
            names = helpers.git(
                self.repo, "ls-tree", "-r", "--name-only", ref
            ).stdout.decode()
            self.assertIn("good.txt", names)
        finally:
            os.chmod(bad, 0o644)

    def test_odd_filenames(self):
        helpers.write_file(self.repo, "with space.txt", "a")
        helpers.write_file(self.repo, "non_ascii_é.txt", "b")
        helpers.write_file(self.repo, "-leading-dash.txt", "c")
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 0)
        self.assertEqual(res.files, 3)

    def test_empty_repo_returns_one(self):
        res = engine.capture(self.repo, via="manual")
        self.assertEqual(res.status, 1)

    def test_non_repo_returns_two(self):
        import tempfile
        import shutil

        d = tempfile.mkdtemp(prefix="not-a-repo-")
        try:
            res = engine.capture(d, via="manual")
            self.assertEqual(res.status, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_state_files_in_git_dir_not_worktree(self):
        helpers.write_file(self.repo, "a.txt", "a")
        engine.capture(self.repo, via="manual")
        # mtime cache must NOT appear in the work tree / ls-files.
        names = helpers.git(
            self.repo, "ls-files", "--others", "--exclude-standard"
        ).stdout.decode()
        self.assertNotIn("mtime-cache.json", names)
        self.assertTrue(
            os.path.exists(os.path.join(self.repo, ".git", "snap", "mtime-cache.json"))
        )

    def test_restore_backs_up_existing(self):
        helpers.write_file(self.repo, "f.txt", "v1")
        res = engine.capture(self.repo, via="manual")
        helpers.write_file(self.repo, "f.txt", "v2-current")
        status, _ = engine.restore_file(self.repo, res.id, "f.txt")
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"v1")
        # Current content stashed under .snap-bak.
        bak = os.path.join(self.repo, ".snap-bak", "f.txt")
        self.assertTrue(os.path.exists(bak))
        with open(bak, "rb") as fh:
            self.assertEqual(fh.read(), b"v2-current")

    def test_restore_missing_path_returns_three(self):
        helpers.write_file(self.repo, "f.txt", "v1")
        res = engine.capture(self.repo, via="manual")
        status, _ = engine.restore_file(self.repo, res.id, "nope.txt")
        self.assertEqual(status, 3)

    def test_restore_unknown_id_returns_three(self):
        status, _ = engine.restore_file(self.repo, "19990101-000000-0001-1", "x")
        self.assertEqual(status, 3)
from tests.helpers import _ls_tree_modes


class CaptureInterruptTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_keyboardinterrupt_releases_lock_and_cleans_tmp_index(self):
        helpers.write_file(self.repo, "f.txt", "x")
        real_out = gitutil.git_out

        def boom(repo, args, **kw):
            if args[:1] == ["write-tree"]:
                raise KeyboardInterrupt()
            return real_out(repo, args, **kw)

        with mock.patch.object(gitutil, "git_out", side_effect=boom):
            with self.assertRaises(KeyboardInterrupt):
                engine.capture(self.repo, via="manual", force=True)
        # tmp capture index must be cleaned and the lock released: a normal
        # capture right after must succeed (#21).
        snap = gitutil.snap_dir(self.repo)
        leftovers = [n for n in os.listdir(snap) if n.startswith("index.capture.")]
        self.assertEqual(leftovers, [], "tmp capture index leaked")
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, "lock not released after interrupt")


class StagedCaptureIndexUntouchedTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_real_index_byte_identical_after_capture(self):
        helpers.write_file(self.repo, "f.txt", "BASE")
        helpers.git(self.repo, "add", "f.txt")
        helpers.git(self.repo, "commit", "-qm", "base")
        helpers.write_file(self.repo, "f.txt", "STAGED_V1")
        helpers.git(self.repo, "add", "f.txt")          # real index = STAGED_V1
        helpers.write_file(self.repo, "f.txt", "WORKTREE_V2")

        index_path = gitutil.git_path(self.repo, "index")
        with open(index_path, "rb") as fh:
            before = fh.read()
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0)
        with open(index_path, "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after, "real .git/index must be byte-identical (C3)")
        # staged content still STAGED_V1 in the real index
        staged = helpers.git(self.repo, "show", ":f.txt").stdout
        self.assertEqual(staged, b"STAGED_V1")
        # no leftover staged temp index
        snap = gitutil.snap_dir(self.repo)
        leftovers = [n for n in os.listdir(snap) if n.startswith("index.staged.")]
        self.assertEqual(leftovers, [], "staged temp index leaked")


# ------------------------------------------------------------------------- D


class ModeFidelityTest(unittest.TestCase):
    """M9 / M11: exec-bit negative direction and symlink stored as 120000."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_non_executable_restores_without_exec_bit(self):
        helpers.write_file(self.repo, "plain.txt", "x", executable=False)
        snap = engine.capture(self.repo, via="manual", force=True)
        os.unlink(os.path.join(self.repo, "plain.txt"))
        engine.restore_file(self.repo, snap.id, "plain.txt")
        st = os.stat(os.path.join(self.repo, "plain.txt"))
        self.assertEqual(st.st_mode & 0o111, 0, "plain file came back executable")

    def test_capture_stores_symlink_as_mode_120000_not_inlined(self):
        helpers.write_file(self.repo, "target.txt", "TARGET BYTES")
        os.symlink("target.txt", os.path.join(self.repo, "link"))
        snap = engine.capture(self.repo, via="manual", force=True)
        ref = engine.resolve_ref(snap.id)
        mode, _sha = engine._path_mode_sha_in_ref(self.repo, ref, "link")
        self.assertEqual(mode, "120000", "symlink not stored as a symlink")
        blob = helpers.git(self.repo, "cat-file", "blob", "{0}:link".format(ref)).stdout
        self.assertEqual(blob, b"target.txt", "link target was followed/inlined")


class RestoreTmpNotCapturedTest(unittest.TestCase):
    """L4: a leftover .snap-restore-tmp must never be snapshotted as noise."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_tmp_excluded_from_capture(self):
        helpers.write_file(self.repo, "real.txt", "r")
        helpers.write_file(
            self.repo, "real.txt" + gitutil.SNAP_RESTORE_TMP_SUFFIX, "leftover"
        )
        files, _dropped = gitutil.list_worktree_files(self.repo)
        self.assertIn("real.txt", files)
        self.assertNotIn("real.txt" + gitutil.SNAP_RESTORE_TMP_SUFFIX, files)


class TrackedDeletedTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _commit(self, rel, content):
        helpers.write_file(self.repo, rel, content)
        helpers.git(self.repo, "add", rel)
        helpers.git(self.repo, "commit", "-qm", "add " + rel)

    def test_tracked_deleted_is_captured_and_restores_original(self):
        self._commit("keep.txt", "IMPORTANT committed content")
        os.unlink(os.path.join(self.repo, "keep.txt"))  # rm, still staged
        helpers.write_file(self.repo, "other.txt", "x")  # so capture has work
        res = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(res.status, 0)
        ref = engine.resolve_ref(res.id)
        names = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", ref
        ).stdout.decode()
        self.assertIn("keep.txt", names, "deleted-but-tracked file must be kept")
        status, _ = engine.restore_file(self.repo, res.id, "keep.txt")
        self.assertEqual(status, 0)
        self.assertEqual(
            helpers.read_file(self.repo, "keep.txt"),
            b"IMPORTANT committed content",
            "restored content must be the pre-deletion blob, not empty",
        )

    def test_tracked_deleted_symlink_restores_as_symlink(self):
        helpers.write_file(self.repo, "target.txt", "real")
        os.symlink("target.txt", os.path.join(self.repo, "link"))
        helpers.git(self.repo, "add", "-A")
        helpers.git(self.repo, "commit", "-qm", "add link")
        os.unlink(os.path.join(self.repo, "link"))  # delete the symlink
        helpers.write_file(self.repo, "other.txt", "x")
        res = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(res.status, 0)
        status, _ = engine.restore_file(self.repo, res.id, "link")
        self.assertEqual(status, 0)
        self.assertTrue(os.path.islink(os.path.join(self.repo, "link")))
        self.assertEqual(os.readlink(os.path.join(self.repo, "link")), "target.txt")

    def test_tracked_secret_is_captured_untracked_secret_is_not(self):
        # The secret floor exempts TRACKED files -- a committed .env is
        # already in the object store, so snapshotting it (incl. via the
        # deleted-injection path) leaks nothing and excluding it would only create
        # a protection gap. An UNTRACKED secret is still dropped by the floor.
        self._commit(".env", "API_KEY=supersecret")          # tracked secret
        os.unlink(os.path.join(self.repo, ".env"))           # rm, still staged
        helpers.write_file(self.repo, "other.txt", "x")
        helpers.write_file(self.repo, ".env.local", "LOCAL=untracked-secret")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(res.status, 0)
        ref = engine.resolve_ref(res.id)
        names = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", ref
        ).stdout.decode()
        self.assertIn(
            ".env", names,
            "tracked secret is exempt from the floor and must be captured (H2)",
        )
        self.assertNotIn(
            ".env.local", names,
            "untracked secret must still be dropped by the floor",
        )


class SnapBakNoRecaptureTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_snap_bak_not_recaptured(self):
        helpers.write_file(self.repo, "f.txt", "v1")
        s1 = engine.capture(self.repo, via="manual", debounce=0)
        helpers.write_file(self.repo, "f.txt", "v2-current")
        engine.restore_file(self.repo, s1.id, "f.txt")  # makes .snap-bak/f.txt
        self.assertTrue(
            os.path.exists(os.path.join(self.repo, ".snap-bak", "f.txt"))
        )
        s2 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        ref = engine.resolve_ref(s2.id)
        names = helpers.git(
            self.repo, "ls-tree", "-r", "--name-only", ref
        ).stdout.decode()
        self.assertNotIn(".snap-bak", names, "snap-bak must never be captured")

    def test_snap_bak_excluded_from_enumeration(self):
        helpers.write_file(self.repo, ".snap-bak/old/x.txt", "stale backup")
        helpers.write_file(self.repo, "real.txt", "real")
        files, _ = engine.gitutil.list_worktree_files(self.repo)
        self.assertIn("real.txt", files)
        self.assertFalse(
            any(p.startswith(".snap-bak") for p in files),
            "list_worktree_files must drop .snap-bak paths",
        )


class SubmoduleGitlinkTest(unittest.TestCase):
    """A: a live submodule pin must survive a snapshot and be recoverable."""

    def setUp(self):
        self.repo = helpers.make_repo()
        # Build a standalone repo to use as the submodule source, with a known
        # commit to pin.
        self.sub_src = helpers.make_repo()
        helpers.write_file(self.sub_src, "lib.txt", "submodule content\n")
        helpers.git(self.sub_src, "add", "-A")
        helpers.git(self.sub_src, "commit", "-q", "-m", "sub init")
        self.sub_commit = helpers.git(
            self.sub_src, "rev-parse", "HEAD"
        ).stdout.decode("ascii").strip()
        # Add it as a submodule of the main repo (file:// + protocol allow so
        # local submodule add works under test).
        helpers.write_file(self.repo, "main.txt", "super content\n")
        helpers.git(
            self.repo, "-c", "protocol.file.allow=always", "submodule", "add",
            self.sub_src, "sub", check=True,
        )

    def tearDown(self):
        helpers.cleanup(self.repo)
        helpers.cleanup(self.sub_src)

    def test_gitlink_recorded_in_snapshot(self):
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        modes = _ls_tree_modes(self.repo, res.id)
        self.assertIn("sub", modes, "submodule path missing from snapshot tree")
        self.assertEqual(modes["sub"], "160000", "submodule not stored as gitlink")
        # The pinned commit must match the submodule's live HEAD.
        ref = engine.resolve_ref(res.id)
        ls = helpers.git(self.repo, "ls-tree", ref, "--", "sub").stdout.decode()
        self.assertIn(self.sub_commit, ls)
        # Regular files still captured alongside the gitlink.
        self.assertIn("main.txt", modes)
        self.assertEqual(modes["main.txt"], "100644")

    def test_restore_file_surfaces_pin_without_crash(self):
        res = engine.capture(self.repo, via="manual", force=True)
        status, msg = engine.restore_file(self.repo, res.id, "sub")
        # Not a crash, not a "file not in snapshot": a clear submodule advisory.
        self.assertEqual(status, 0, msg)
        self.assertIn("submodule", msg)
        self.assertIn(self.sub_commit, msg)

    def test_restore_all_handles_gitlink(self):
        res = engine.capture(self.repo, via="manual", force=True)
        # Mutate a regular file so restore_all has real blob work to do too.
        helpers.write_file(self.repo, "main.txt", "LOCAL EDIT\n")
        status, msg = engine.restore_all(self.repo, res.id, no_backup=True)
        self.assertEqual(status, 0, msg)
        # The blob was restored; the gitlink was not auto-applied (submodule
        # working tree untouched) but did not abort the batch.
        self.assertEqual(helpers.read_file(self.repo, "main.txt"), b"super content\n")

    def test_uninitialized_submodule_uses_staged_pin_not_superproject_head(self):
        # An empty (deinit'd) submodule must record the staged index
        # pin, NOT the superproject's HEAD (which `rev-parse` in an empty dir
        # would wrongly return).
        helpers.git(self.repo, "add", "-A")
        helpers.git(self.repo, "commit", "-q", "-m", "add submodule")
        super_head = helpers.git(
            self.repo, "rev-parse", "HEAD"
        ).stdout.decode("ascii").strip()
        self.assertNotEqual(super_head, self.sub_commit)
        helpers.git(self.repo, "submodule", "deinit", "-f", "sub")
        self.assertEqual(os.listdir(os.path.join(self.repo, "sub")), [],
                         "deinit should leave an empty submodule dir")
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        ref = engine.resolve_ref(res.id)
        ls = helpers.git(self.repo, "ls-tree", ref, "--", "sub").stdout.decode()
        self.assertIn(self.sub_commit, ls, "staged pin not recorded")
        self.assertNotIn(super_head, ls, "wrongly recorded superproject HEAD")

    def test_embedded_repo_not_recorded_as_submodule(self):
        # An untracked embedded git repo is listed as a directory but
        # is not a tracked submodule; it must be skipped, not recorded as a pin.
        emb = os.path.join(self.repo, "embedded")
        os.makedirs(emb)
        helpers.git(emb, "init", "-q")
        helpers.git(emb, "config", "user.email", "t@local")
        helpers.git(emb, "config", "user.name", "t")
        helpers.write_file(self.repo, "embedded/x.txt", "hi\n")
        helpers.git(emb, "add", "-A")
        helpers.git(emb, "commit", "-q", "-m", "e")
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        modes = _ls_tree_modes(self.repo, res.id)
        self.assertNotIn("embedded", modes)
        self.assertNotIn("embedded/", modes)
        # The real submodule and file are still captured.
        self.assertEqual(modes.get("sub"), "160000")
        self.assertIn("main.txt", modes)

if __name__ == "__main__":
    unittest.main()
