#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Restore UX: diff (name-status / per-file), restore --dir / --all, list."""
import os
import unittest

from tests import helpers
from git_safepoint import engine

import contextlib
import io
import shutil
import subprocess
import tempfile
from contextlib import redirect_stderr
from unittest import mock

from git_safepoint import gitutil, cli
from git_safepoint.lock import RepoLock


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_name_status_between_snapshots(self):
        helpers.write_file(self.repo, "keep.txt", "k")
        helpers.write_file(self.repo, "gone.txt", "g")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        # mutate: modify keep, delete gone, add added
        helpers.write_file(self.repo, "keep.txt", "k-changed")
        os.unlink(os.path.join(self.repo, "gone.txt"))
        helpers.write_file(self.repo, "added.txt", "a")
        s2 = engine.capture(self.repo, via="manual", debounce=0, force=True)

        status, text = engine.diff_name_status(self.repo, s1.id, s2.id)
        self.assertEqual(status, 0)
        # A=added, M=modified, D=deleted
        self.assertIn("A\tadded.txt", text)
        self.assertIn("M\tkeep.txt", text)
        self.assertIn("D\tgone.txt", text)

    def test_diff_unknown_id_returns_three(self):
        helpers.write_file(self.repo, "f.txt", "x")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        status, _ = engine.diff_name_status(self.repo, s1.id, "nope-id")
        self.assertEqual(status, 3)

    def test_diff_against_worktree(self):
        helpers.write_file(self.repo, "f.txt", "v1")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        helpers.write_file(self.repo, "f.txt", "v2")
        helpers.write_file(self.repo, "new.txt", "n")
        status, text = engine.diff_name_status(self.repo, s1.id, None)
        self.assertEqual(status, 0)
        # f.txt modified relative to the snapshot
        self.assertIn("f.txt", text)

    def test_per_file_unified_diff(self):
        helpers.write_file(self.repo, "f.txt", "line1\nline2\n")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        helpers.write_file(self.repo, "f.txt", "line1\nCHANGED\n")
        s2 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        status, text = engine.diff_file(self.repo, s1.id, "f.txt", s2.id)
        self.assertEqual(status, 0)
        self.assertIn("-line2", text)
        self.assertIn("+CHANGED", text)


class RestoreDirAllTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_dir_subtree(self):
        helpers.write_file(self.repo, "src/a.py", "a")
        helpers.write_file(self.repo, "src/b.py", "b")
        helpers.write_file(self.repo, "docs/c.md", "c")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        # wipe src
        os.unlink(os.path.join(self.repo, "src/a.py"))
        os.unlink(os.path.join(self.repo, "src/b.py"))
        status, msg = engine.restore_dir(self.repo, s1.id, "src")
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "src/a.py"), b"a")
        self.assertEqual(helpers.read_file(self.repo, "src/b.py"), b"b")

    def test_restore_dir_index_untouched(self):
        helpers.write_file(self.repo, "src/a.py", "a")
        helpers.git(self.repo, "add", "src/a.py")
        helpers.git(self.repo, "commit", "-qm", "init")
        helpers.write_file(self.repo, "src/b.py", "b")  # untracked
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        os.unlink(os.path.join(self.repo, "src/b.py"))
        # The user's index/tracked state must be unchanged by the restore (only
        # the file content comes back). Compare the index dimension before/after
        # rather than full `status --porcelain`, which would also see the restored
        # untracked b.py reappear.
        idx_before = helpers.git(
            self.repo, "diff", "--cached", "--name-only"
        ).stdout
        engine.restore_dir(self.repo, s1.id, "src")
        idx_after = helpers.git(
            self.repo, "diff", "--cached", "--name-only"
        ).stdout
        self.assertEqual(idx_before, idx_after, "restore must not touch the index")
        self.assertEqual(idx_after, b"")

    def test_restore_all(self):
        helpers.write_file(self.repo, "a.txt", "a")
        helpers.write_file(self.repo, "b/c.txt", "c")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        os.unlink(os.path.join(self.repo, "a.txt"))
        os.unlink(os.path.join(self.repo, "b/c.txt"))
        status, msg = engine.restore_all(self.repo, s1.id)
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "a.txt"), b"a")
        self.assertEqual(helpers.read_file(self.repo, "b/c.txt"), b"c")

    def test_restore_dir_missing_returns_three(self):
        helpers.write_file(self.repo, "a.txt", "a")
        s1 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        status, _ = engine.restore_dir(self.repo, s1.id, "nonexistent")
        self.assertEqual(status, 3)


class ListMetaTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_list_newest_first_and_meta(self):
        for i in range(3):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            engine.capture(self.repo, via="manual", debounce=0, force=True,
                           label="lbl{0}".format(i))
        rows = engine.list_refs(self.repo)
        self.assertEqual(len(rows), 3)
        # newest first => ids sorted descending
        self.assertEqual(rows, sorted(rows, reverse=True))
        sid, ref = rows[0]
        meta = engine.snapshot_meta(self.repo, sid, ref)
        for key in ("id", "ref", "commit", "files", "bytes", "label", "via", "epoch"):
            self.assertIn(key, meta)
        self.assertEqual(meta["via"], "manual")
        self.assertTrue(meta["label"].startswith("lbl"))


class SnapBakRegularFileTest(unittest.TestCase):
    """A `.snap-bak` that is a regular file is reported clearly and
    the restore refuses up front rather than clobbering the local file."""

    def test_regular_file_snap_bak_refuses_with_clear_message(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "v1")
            engine.capture(repo, via="manual", force=True, debounce=0)
            snap_id = engine.list_refs(repo)[0][0]
            with open(os.path.join(repo, gitutil.SNAP_BAK_DIR), "w") as fh:
                fh.write("x")  # .snap-bak is a regular file, not a dir
            helpers.write_file(repo, "f.txt", "v2-local")
            status, msg = engine.restore_file(repo, snap_id, "f.txt")
            self.assertEqual(status, 2)
            self.assertIn(gitutil.SNAP_BAK_DIR, msg)
            # The local file must be untouched (restore refused before writing).
            self.assertEqual(helpers.read_file(repo, "f.txt"), b"v2-local")
        finally:
            helpers.cleanup(repo)


class RestoreBackupSurfacingTest(unittest.TestCase):
    """N1/N4: restore backs up overwritten work and surfaces .snap-bak."""

    def test_restore_file_drift_backs_up_old_content(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "v1")
            engine.capture(repo, via="manual", force=True, debounce=0)
            sid = engine.list_refs(repo)[0][0]
            helpers.write_file(repo, "f.txt", "v2-local")
            status, _msg = engine.restore_file(repo, sid, "f.txt")
            self.assertEqual(status, 0)
            self.assertEqual(helpers.read_file(repo, "f.txt"), b"v1")
            bak = os.path.join(repo, gitutil.SNAP_BAK_DIR, "f.txt")
            self.assertTrue(os.path.exists(bak))
            self.assertEqual(helpers.read_file(repo, gitutil.SNAP_BAK_DIR + "/f.txt"),
                             b"v2-local")
        finally:
            helpers.cleanup(repo)

    def test_restore_all_message_mentions_snap_bak(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "v1")
            engine.capture(repo, via="manual", force=True, debounce=0)
            sid = engine.list_refs(repo)[0][0]
            helpers.write_file(repo, "f.txt", "v2-local")
            status, msg = engine.restore_all(repo, sid, no_backup=False)
            self.assertEqual(status, 0, msg)
            self.assertIn(gitutil.SNAP_BAK_DIR, msg)
        finally:
            helpers.cleanup(repo)


class RestoreUxTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_directory_path_suggests_dir(self):
        helpers.write_file(self.repo, "notes/a.txt", "a")
        helpers.write_file(self.repo, "notes/b.txt", "b")
        engine.capture(self.repo, force=True)
        sid = engine.list_refs(self.repo)[0][0]
        status, msg = engine.restore_file(self.repo, sid, "notes")
        self.assertEqual(status, 2, msg)
        self.assertIn("--dir", msg)

    def test_restore_dir_dot_restores_whole_tree(self):
        helpers.write_file(self.repo, "notes/a.txt", "a")
        helpers.write_file(self.repo, "top.txt", "t")
        engine.capture(self.repo, force=True)
        sid = engine.list_refs(self.repo)[0][0]
        os.remove(os.path.join(self.repo, "notes", "a.txt"))
        os.remove(os.path.join(self.repo, "top.txt"))
        for dot in (".", "./"):
            status, msg = engine.restore_dir(self.repo, sid, dot)
            self.assertEqual(status, 0, "{0}: {1}".format(dot, msg))
            self.assertEqual(helpers.read_file(self.repo, "notes/a.txt"), b"a")
            self.assertEqual(helpers.read_file(self.repo, "top.txt"), b"t")
            os.remove(os.path.join(self.repo, "top.txt"))  # reset for next dot


# --- diff-vs-work-tree on git < 2.25 reports, not silently empty -----


class DiffOldGitTest(unittest.TestCase):
    def test_add_failure_reports_error_not_empty_diff(self):
        repo = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        helpers.write_file(repo, "f.txt", "v1")
        engine.capture(repo, force=True)
        sid = engine.list_refs(repo)[0][0]
        helpers.write_file(repo, "f.txt", "v2")  # a real difference exists

        orig = gitutil.run_git

        def fake(repo_, args, **kw):
            # Simulate old git rejecting --pathspec-file-nul on `git add`.
            if args[:2] == ["add", "-A"]:
                return subprocess.CompletedProcess(
                    args, 128, stdout=b"",
                    stderr=b"error: unknown option `pathspec-file-nul'",
                )
            return orig(repo_, args, **kw)

        gitutil.run_git = fake
        try:
            status, text = engine.diff_name_status(repo, sid, None)
        finally:
            gitutil.run_git = orig
        self.assertEqual(status, 2, text)
        self.assertIn("2.25", text)


# --- list survives a ref pruned concurrently mid-listing -------------


class NitFixesTest(unittest.TestCase):
    def test_restore_through_in_repo_symlinked_parent_allowed(self):
        repo = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        helpers.write_file(repo, "realsub/code.py", "v1")
        helpers.write_file(repo, "actual/.keep", "")
        engine.capture(repo, force=True)
        sid = engine.list_refs(repo)[0][0]
        # Replace realsub/ with an IN-REPO symlink to actual/, and lose the file.
        os.remove(os.path.join(repo, "realsub", "code.py"))
        os.rmdir(os.path.join(repo, "realsub"))
        os.symlink("actual", os.path.join(repo, "realsub"))
        status, msg = engine.restore_file(repo, sid, "realsub/code.py")
        self.assertEqual(status, 0, msg)  # #15: in-repo symlink no longer refused
        self.assertEqual(helpers.read_file(repo, "actual/code.py"), b"v1")

    def test_restore_through_escaping_symlinked_parent_still_refused(self):
        repo = helpers.make_repo()
        outside = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        self.addCleanup(helpers.cleanup, outside)
        helpers.write_file(repo, "sub/secret.txt", "in-repo")
        engine.capture(repo, force=True)
        sid = engine.list_refs(repo)[0][0]
        sub = os.path.join(repo, "sub")
        os.remove(os.path.join(sub, "secret.txt"))
        os.rmdir(sub)
        os.symlink(outside, sub)  # ESCAPING symlink
        status, _ = engine.restore_file(repo, sid, "sub/secret.txt")
        self.assertNotEqual(status, 0)  # still refused
        self.assertFalse(os.path.exists(os.path.join(outside, "secret.txt")))

    def test_pathspec_env_vars_scrubbed(self):
        for k in ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
                  "GIT_ICASE_PATHSPECS", "GIT_LITERAL_PATHSPECS"):
            self.assertIn(k, gitutil._GIT_CONTEXT_ENV)  # #18

    def test_list_limit_rejects_negative(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):  # #19
            parser.parse_args(["list", "--limit", "-1"])


class C3StagedVariantTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _commit(self, rel, content):
        helpers.write_file(self.repo, rel, content)
        helpers.git(self.repo, "add", rel)
        helpers.git(self.repo, "commit", "-qm", "add " + rel)

    def test_staged_variant_captured_and_restorable(self):
        # HEAD=BASE, index=STAGED_V1, work tree=WORKTREE_V2: the staged version
        # exists nowhere reachable and dies on reset --hard.
        self._commit("f.txt", "BASE")
        helpers.write_file(self.repo, "f.txt", "STAGED_V1")
        helpers.git(self.repo, "add", "f.txt")
        helpers.write_file(self.repo, "f.txt", "WORKTREE_V2")

        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        meta = engine.snapshot_meta(
            self.repo, res.id, engine.resolve_ref(res.id)
        )
        self.assertTrue(meta["has_staged"], "staged variant must be recorded")

        # Normal restore recovers the work-tree version.
        st, msg = engine.restore_file(self.repo, res.id, "f.txt", no_backup=True)
        self.assertEqual(st, 0, msg)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"WORKTREE_V2")

        # --staged recovers the otherwise-unrecoverable staged version.
        st, msg = engine.restore_file(
            self.repo, res.id, "f.txt", no_backup=True, staged=True
        )
        self.assertEqual(st, 0, msg)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"STAGED_V1")

    def test_no_staged_parent_for_untracked_only(self):
        # Only an untracked file present: index == work tree (tracked), so NO
        # redundant staged parent (the bug the over-trigger would have caused).
        self._commit("f.txt", "X")
        helpers.write_file(self.repo, "untracked.txt", "Y")
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        meta = engine.snapshot_meta(
            self.repo, res.id, engine.resolve_ref(res.id)
        )
        self.assertFalse(meta["has_staged"], "untracked files must not add a parent")
        st, _ = engine.restore_all(
            self.repo, res.id, no_backup=True, staged=True
        )
        self.assertEqual(st, 3, "no staged variant -> status 3")

    def test_no_staged_parent_for_unstaged_only(self):
        # Unstaged edit only (index == HEAD): recoverable from the work-tree tree,
        # so no staged parent.
        self._commit("f.txt", "BASE")
        helpers.write_file(self.repo, "f.txt", "EDITED_UNSTAGED")
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        meta = engine.snapshot_meta(
            self.repo, res.id, engine.resolve_ref(res.id)
        )
        self.assertFalse(meta["has_staged"])


# --------------------------------------------------------------------------- C7


class RestoreSafetyLowsTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_does_not_clobber_via_leftover_tmp_symlink(self):
        # A leftover ``<path>.snap-restore-tmp`` symlink pointing outside the repo
        # must not be followed by the regular-file write (review: restore tmp
        # symlink hazard); the regular branch now unlinks tmp first like the
        # symlink branch.
        helpers.write_file(self.repo, "f.txt", "SNAP")
        res = engine.capture(self.repo, via="manual", force=True)
        outside = tempfile.mkdtemp(prefix="git-safepoint-victim-")
        self.addCleanup(shutil.rmtree, outside, True)
        sentinel = os.path.join(outside, "victim")
        with open(sentinel, "wb") as fh:
            fh.write(b"PRECIOUS")
        tmp = os.path.join(self.repo, "f.txt" + gitutil.SNAP_RESTORE_TMP_SUFFIX)
        os.symlink(sentinel, tmp)
        helpers.write_file(self.repo, "f.txt", "DIRTY")
        status, msg = engine.restore_file(
            self.repo, res.id, "f.txt", no_backup=True
        )
        self.assertEqual(status, 0, msg)
        with open(sentinel, "rb") as fh:
            self.assertEqual(fh.read(), b"PRECIOUS", "out-of-repo file clobbered")
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"SNAP")

    def test_restore_all_skips_fifo_path_without_hanging(self):
        # no_backup=False: _backup_existing refuses the live FIFO (can't stream it)
        # -> that one file is skipped (not hung), the others restore.
        if not hasattr(os, "mkfifo"):
            self.skipTest("platform has no mkfifo")
        import stat as _stat
        helpers.write_file(self.repo, "a.txt", "A")
        helpers.write_file(self.repo, "b.txt", "B")
        res = engine.capture(self.repo, via="manual", force=True)
        os.unlink(os.path.join(self.repo, "a.txt"))
        os.unlink(os.path.join(self.repo, "b.txt"))
        os.mkfifo(os.path.join(self.repo, "b.txt"))  # live path is now a FIFO
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # Pre-fix _backup_existing's open(fifo, "rb") blocked FOREVER here.
            status, msg = engine.restore_all(self.repo, res.id)
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "a.txt"), b"A", "other files restored")
        self.assertIn("skipped", err.getvalue())
        self.assertIn("b.txt", err.getvalue())
        # The FIFO was NOT overwritten (backup refused -> skip), still a FIFO.
        self.assertTrue(_stat.S_ISFIFO(os.lstat(os.path.join(self.repo, "b.txt")).st_mode))

    def test_restore_all_no_backup_overwrites_fifo_path(self):
        # no_backup=True: _backup_existing returns early, so _write_blob_to_path's
        # atomic os.replace overwrites the live FIFO with the snapshot's regular
        # file. The asymmetric (no-backup) branch.
        if not hasattr(os, "mkfifo"):
            self.skipTest("platform has no mkfifo")
        import stat as _stat
        helpers.write_file(self.repo, "b.txt", "B")
        res = engine.capture(self.repo, via="manual", force=True)
        os.unlink(os.path.join(self.repo, "b.txt"))
        os.mkfifo(os.path.join(self.repo, "b.txt"))
        status, msg = engine.restore_all(self.repo, res.id, no_backup=True)
        self.assertEqual(status, 0, msg)
        st = os.lstat(os.path.join(self.repo, "b.txt"))
        self.assertTrue(_stat.S_ISREG(st.st_mode), "FIFO replaced by regular file")
        self.assertEqual(helpers.read_file(self.repo, "b.txt"), b"B")


# ------------------------------------------------------------ prune clock-skew


class RestoreLockDegradationTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_proceeds_when_lock_held(self):
        helpers.write_file(self.repo, "f.txt", "SNAP")
        res = engine.capture(self.repo, via="manual", force=True)
        helpers.write_file(self.repo, "f.txt", "DIRTY")
        lock_path = os.path.join(gitutil.shared_snap_dir(self.repo), "lock")
        holder = RepoLock(lock_path)
        holder.acquire()
        err = io.StringIO()
        try:
            with mock.patch.object(engine, "_RESTORE_LOCK_TIMEOUT", 0.1):
                with contextlib.redirect_stderr(err):
                    status, msg = engine.restore_all(
                        self.repo, res.id, no_backup=True
                    )
        finally:
            holder.release()
        self.assertEqual(status, 0, msg)
        self.assertIn("without it", err.getvalue())
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"SNAP")

    def test_restore_entries_skips_timed_out_file(self):
        helpers.write_file(self.repo, "a.txt", "A")
        helpers.write_file(self.repo, "b.txt", "B")
        res = engine.capture(self.repo, via="manual", force=True)
        os.unlink(os.path.join(self.repo, "a.txt"))
        os.unlink(os.path.join(self.repo, "b.txt"))
        entries = engine._paths_under(self.repo, engine.resolve_ref(res.id), "")
        real_write = engine._write_blob_to_path

        def flaky(repo, spec, rel, mode):
            if rel == "b.txt":
                raise subprocess.TimeoutExpired(cmd="git", timeout=1)
            return real_write(repo, spec, rel, mode)

        err = io.StringIO()
        with mock.patch.object(engine, "_write_blob_to_path", side_effect=flaky):
            with contextlib.redirect_stderr(err):
                restored, skipped = engine._restore_entries(
                    self.repo, entries, no_backup=True
                )
        self.assertEqual((restored, skipped), (1, 1))
        self.assertIn("skipped", err.getvalue())


# ----------------------------------------------------- capture #21 interrupt


class DiffAgainstWorktree(unittest.TestCase):
    """H6/M4: snapshot-vs-worktree diff for untracked / newly-added files."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_diff_file_untracked_shows_content_not_deleted(self):
        helpers.write_file(self.repo, "u.txt", "original work\n")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        snap = engine.list_refs(self.repo)[0][0]
        helpers.write_file(self.repo, "u.txt", "modified now\n")
        status, text = engine.diff_file(self.repo, snap, "u.txt")
        self.assertEqual(status, 0)
        self.assertIn("original work", text)
        self.assertIn("modified now", text)
        self.assertNotIn("/dev/null", text)

    def test_name_status_includes_new_untracked_file(self):
        helpers.write_file(self.repo, "tracked.txt", "v1\n")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        snap = engine.list_refs(self.repo)[0][0]
        helpers.write_file(self.repo, "tracked.txt", "v2\n")
        helpers.write_file(self.repo, "brand_new.txt", "added\n")
        status, text = engine.diff_name_status(self.repo, snap, None)
        self.assertEqual(status, 0)
        self.assertIn("brand_new.txt", text)


class RestoreScopeAndBackup(unittest.TestCase):
    """M1/M2/M5: restore scope exclusivity and backup safety."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _snap_one(self, rel="f.txt", content="SNAP"):
        helpers.write_file(self.repo, rel, content)
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        return engine.list_refs(self.repo)[0][0]

    def test_restore_rejects_multiple_scopes(self):
        from git_safepoint import cli

        snap = self._snap_one()
        rc = cli.main(
            ["--repo", self.repo, "restore", snap, "f.txt", "--all", "--yes"]
        )
        self.assertEqual(rc, 2)

    def test_backup_refuses_symlinked_snap_bak(self):
        snap = self._snap_one("f.txt", "snapshot-content")
        helpers.write_file(self.repo, "f.txt", "current-work")
        outside = tempfile.mkdtemp(prefix="snap-escape-")
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.repo, gitutil.SNAP_BAK_DIR))
        status, _msg = engine.restore_file(self.repo, snap, "f.txt")
        self.assertEqual(status, 2)  # refused: backup dest through a symlink
        self.assertEqual(os.listdir(outside), [])  # nothing written outside
        # current work-tree file must be untouched (not overwritten).
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"current-work")

    def test_repeated_restore_does_not_clobber_backup(self):
        snap = self._snap_one("f.txt", "SNAP")
        helpers.write_file(self.repo, "f.txt", "WORK-A")
        engine.restore_file(self.repo, snap, "f.txt")
        helpers.write_file(self.repo, "f.txt", "WORK-B")
        engine.restore_file(self.repo, snap, "f.txt")
        bakdir = os.path.join(self.repo, gitutil.SNAP_BAK_DIR)
        found = set()
        for root, _d, files in os.walk(bakdir):
            for fn in files:
                with open(os.path.join(root, fn), "rb") as fh:
                    found.add(fh.read())
        self.assertIn(b"WORK-A", found)
        self.assertIn(b"WORK-B", found)


class RestoreSnapBakSafety(unittest.TestCase):
    """R10: a symlinked .snap-bak aborts restore with a clear error, not a no-op."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_all_aborts_clearly_on_symlinked_snap_bak(self):
        helpers.write_file(self.repo, "f.txt", "SNAP")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        snap = engine.list_refs(self.repo)[0][0]
        helpers.write_file(self.repo, "f.txt", "current-work")
        outside = tempfile.mkdtemp(prefix="snap-escape-")
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.repo, gitutil.SNAP_BAK_DIR))
        status, msg = engine.restore_all(self.repo, snap)
        self.assertEqual(status, 2)
        self.assertIn("snap-bak", msg)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"current-work")
        # --no-backup bypasses the guard and restores.
        status2, _ = engine.restore_all(self.repo, snap, no_backup=True)
        self.assertEqual(status2, 0)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"SNAP")


class OddNameBulkRestoreTest(unittest.TestCase):
    """C3: bulk restore must handle non-ASCII / newline / space names."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_restore_all_with_non_ascii_and_newline_names(self):
        names = ["aaa.txt", "café.txt", "zzz.txt", "we ird.txt"]
        for n in names:
            helpers.write_file(self.repo, n, "X-" + n)
        snap = engine.capture(self.repo, via="manual", force=True)
        for n in names:
            os.unlink(os.path.join(self.repo, n))
        status, msg = engine.restore_all(self.repo, snap.id)
        self.assertEqual(status, 0, msg)
        for n in names:
            self.assertEqual(
                helpers.read_file(self.repo, n), ("X-" + n).encode("utf-8"),
                "missing/!= after restore: {0}".format(n),
            )

    def test_restore_dir_with_non_ascii_names(self):
        helpers.write_file(self.repo, "dir/café.txt", "c")
        helpers.write_file(self.repo, "dir/plain.txt", "p")
        snap = engine.capture(self.repo, via="manual", force=True)
        os.unlink(os.path.join(self.repo, "dir/café.txt"))
        os.unlink(os.path.join(self.repo, "dir/plain.txt"))
        status, _ = engine.restore_dir(self.repo, snap.id, "dir")
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "dir/café.txt"), b"c")


class RestoreSafetyTest(unittest.TestCase):
    """H11 / H14 / H15: restore-side robustness and path-traversal guard."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_refuses_restore_through_symlinked_parent(self):
        # Capture a file under sub/, then replace sub/ with a symlink pointing
        # outside the repo. Restore must NOT write through the symlink.
        helpers.write_file(self.repo, "sub/secret.txt", "in-repo")
        snap = engine.capture(self.repo, via="manual", force=True)
        outside = helpers.make_repo()  # a separate dir to act as the escape target
        try:
            sub = os.path.join(self.repo, "sub")
            for f in os.listdir(sub):
                os.unlink(os.path.join(sub, f))
            os.rmdir(sub)
            os.symlink(outside, sub)  # sub -> /outside
            status, msg = engine.restore_file(self.repo, snap.id, "sub/secret.txt")
            self.assertNotEqual(status, 0)  # refused
            self.assertFalse(
                os.path.exists(os.path.join(outside, "secret.txt")),
                "restore escaped the repo via a symlinked parent",
            )
        finally:
            helpers.cleanup(outside)

    def test_restore_against_corrupt_object_store_does_not_crash(self):
        helpers.write_file(self.repo, "f.txt", "content")
        snap = engine.capture(self.repo, via="manual", force=True)
        mode, sha = engine._path_mode_sha_in_ref(
            self.repo, engine.resolve_ref(snap.id), "f.txt"
        )
        # Delete the loose object backing the blob.
        loose = os.path.join(self.repo, ".git", "objects", sha[:2], sha[2:])
        if os.path.exists(loose):
            os.unlink(loose)
        os.unlink(os.path.join(self.repo, "f.txt"))
        status, msg = engine.restore_file(self.repo, snap.id, "f.txt")
        self.assertNotEqual(status, 0)  # clean failure, not a traceback
        # No empty/partial file left behind.
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f.txt")))

    def test_one_bad_file_does_not_abort_bulk_restore(self):
        # Two files; corrupt one blob. restore_all must still restore the other
        # and report the bad one as skipped (per-file isolation, like capture).
        helpers.write_file(self.repo, "good.txt", "good")
        helpers.write_file(self.repo, "bad.txt", "bad")
        snap = engine.capture(self.repo, via="manual", force=True)
        _m, bad_sha = engine._path_mode_sha_in_ref(
            self.repo, engine.resolve_ref(snap.id), "bad.txt"
        )
        loose = os.path.join(self.repo, ".git", "objects", bad_sha[:2], bad_sha[2:])
        if os.path.exists(loose):
            os.unlink(loose)
        os.unlink(os.path.join(self.repo, "good.txt"))
        os.unlink(os.path.join(self.repo, "bad.txt"))
        status, msg = engine.restore_all(self.repo, snap.id)
        self.assertEqual(status, 0)
        self.assertEqual(helpers.read_file(self.repo, "good.txt"), b"good")
        self.assertIn("skipped", msg)

    def test_all_skipped_bulk_restore_reports_failure(self):
        # Codex review: if EVERY entry is skipped (all blobs corrupt), bulk
        # restore must report failure, not exit 0 with "restored 0 file(s)".
        helpers.write_file(self.repo, "a.txt", "a")
        helpers.write_file(self.repo, "b.txt", "b")
        snap = engine.capture(self.repo, via="manual", force=True)
        ref = engine.resolve_ref(snap.id)
        for name in ("a.txt", "b.txt"):
            _m, sha = engine._path_mode_sha_in_ref(self.repo, ref, name)
            loose = os.path.join(self.repo, ".git", "objects", sha[:2], sha[2:])
            if os.path.exists(loose):
                os.unlink(loose)
            os.unlink(os.path.join(self.repo, name))
        status, msg = engine.restore_all(self.repo, snap.id)
        self.assertNotEqual(status, 0, msg)
        self.assertIn("restored 0 file(s)", msg)


class StreamingLargeFileTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_large_file_round_trips_via_streaming(self):
        # ~3 MiB of pseudo-binary content exercises the streaming restore path.
        big = bytes((i * 31 + 7) % 256 for i in range(3 * 1024 * 1024))
        helpers.write_file(self.repo, "big.bin", big)
        res = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(res.status, 0)
        os.unlink(os.path.join(self.repo, "big.bin"))
        engine.restore_file(self.repo, res.id, "big.bin")
        self.assertEqual(helpers.read_file(self.repo, "big.bin"), big)

    def test_large_existing_file_backed_up_then_restored(self):
        v1 = bytes((i * 13) % 256 for i in range(2 * 1024 * 1024))
        v2 = bytes((i * 17 + 1) % 256 for i in range(2 * 1024 * 1024))
        helpers.write_file(self.repo, "big.bin", v1)
        res = engine.capture(self.repo, via="manual", debounce=0)
        helpers.write_file(self.repo, "big.bin", v2)
        engine.restore_file(self.repo, res.id, "big.bin")
        self.assertEqual(helpers.read_file(self.repo, "big.bin"), v1)
        bak = os.path.join(self.repo, ".snap-bak", "big.bin")
        with open(bak, "rb") as fh:
            self.assertEqual(fh.read(), v2)


class RestoreDriftWarningTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _restore_capturing_stderr(self, snap_id, path, no_backup=False):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _ = engine.restore_file(
                self.repo, snap_id, path, no_backup=no_backup
            )
        return status, err.getvalue()

    def test_warns_on_drift(self):
        helpers.write_file(self.repo, "g.txt", "snap-content")
        s1 = engine.capture(self.repo, via="manual", debounce=0)
        helpers.write_file(self.repo, "g.txt", "CURRENT UNSAVED WORK")
        status, err = self._restore_capturing_stderr(s1.id, "g.txt")
        self.assertEqual(status, 0)
        self.assertIn("differing from the snapshot", err)
        # The pre-restore content is preserved.
        with open(os.path.join(self.repo, ".snap-bak", "g.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"CURRENT UNSAVED WORK")

    def test_no_warning_when_identical(self):
        helpers.write_file(self.repo, "g.txt", "same")
        s1 = engine.capture(self.repo, via="manual", debounce=0)
        # work-tree file is identical to the snapshot -> no drift warning
        status, err = self._restore_capturing_stderr(s1.id, "g.txt")
        self.assertEqual(status, 0)
        self.assertNotIn("differing", err)

    def test_no_warning_when_file_absent(self):
        helpers.write_file(self.repo, "g.txt", "content")
        s1 = engine.capture(self.repo, via="manual", debounce=0)
        os.unlink(os.path.join(self.repo, "g.txt"))
        status, err = self._restore_capturing_stderr(s1.id, "g.txt")
        self.assertEqual(status, 0)
        self.assertNotIn("differing", err)


class NonUtf8RestoreTest(unittest.TestCase):
    """H: a non-UTF-8 path name must not crash restore / diff."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _make_non_utf8_file(self):
        """Create a file whose name has a raw 0xff byte; skip if the FS refuses."""
        name = b"bad_\xff_name.txt"
        abs_b = os.path.join(self.repo.encode(), name)
        try:
            with open(abs_b, "wb") as fh:
                fh.write(b"payload-v1\n")
        except (OSError, ValueError):
            self.skipTest("filesystem rejects non-UTF-8 names")
        # The repo-relative path as Python sees it (surrogateescape).
        return os.fsdecode(name)

    def _make_ref_with_non_utf8_path(self):
        """Build a snapshot tree containing a 0xff-byte path via git plumbing.

        Filesystem-independent: works even where the FS refuses non-UTF-8 names
        (macOS / APFS), so it covers the strict-decode crash on every platform.
        """
        name = b"bad_\xff_name.txt"
        blob = helpers.git(
            self.repo, "hash-object", "-w", "--stdin", input_bytes=b"payload-v1\n"
        ).stdout.decode("ascii").strip()
        rec = b"100644 " + blob.encode("ascii") + b"\t" + name + b"\x00"
        idx = os.path.join(self.repo, ".git", "tmp-test-index")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = idx
        subprocess.run(
            ["git", "-C", self.repo, "update-index", "-z", "--index-info"],
            input=rec, env=env, check=True,
        )
        tree = subprocess.run(
            ["git", "-C", self.repo, "write-tree"],
            env=env, stdout=subprocess.PIPE, check=True,
        ).stdout.decode("ascii").strip()
        commit = helpers.git(
            self.repo, "commit-tree", tree, "-m", "snap"
        ).stdout.decode("ascii").strip()
        helpers.git(self.repo, "update-ref", "refs/snapshots/TESTSNAP", commit)
        os.unlink(idx)
        return "refs/snapshots/TESTSNAP", os.fsdecode(name)

    def test_path_mode_sha_in_ref_non_utf8_plumbing(self):
        # Platform-independent: pre-fix this raised UnicodeDecodeError.
        ref, rel = self._make_ref_with_non_utf8_path()
        mode, sha = engine._path_mode_sha_in_ref(self.repo, ref, rel)
        self.assertEqual(mode, "100644")
        self.assertTrue(sha)

    def test_path_mode_sha_in_ref_no_crash(self):
        rel = self._make_non_utf8_file()
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0, res.reason)
        ref = engine.resolve_ref(res.id)
        # Pre-fix this raised UnicodeDecodeError on the strict decode.
        mode, sha = engine._path_mode_sha_in_ref(self.repo, ref, rel)
        self.assertEqual(mode, "100644")
        self.assertTrue(sha)

    def test_restore_file_roundtrip_non_utf8(self):
        rel = self._make_non_utf8_file()
        res = engine.capture(self.repo, via="manual", force=True)
        # Overwrite the live file, then restore it from the snapshot.
        abs_b = os.path.join(self.repo.encode(), os.fsencode(rel))
        with open(abs_b, "wb") as fh:
            fh.write(b"payload-v2-clobbered\n")
        status, msg = engine.restore_file(self.repo, res.id, rel, no_backup=True)
        self.assertEqual(status, 0, msg)
        with open(abs_b, "rb") as fh:
            self.assertEqual(fh.read(), b"payload-v1\n")


class RestoreInterruptTest(unittest.TestCase):
    """F: an interrupt mid batch-restore reports progress and re-raises."""

    def setUp(self):
        self.repo = helpers.make_repo()
        for i in range(6):
            helpers.write_file(self.repo, "d/f{0}.txt".format(i), "v1-{0}".format(i))
        self.snap = engine.capture(self.repo, via="manual", force=True)
        # Clobber every file so restore has real work to do.
        for i in range(6):
            helpers.write_file(self.repo, "d/f{0}.txt".format(i), "CLOBBERED")

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_interrupt_reports_partial_progress_and_reraises(self):
        ref = engine.resolve_ref(self.snap.id)
        entries = engine._paths_under(self.repo, ref, "")
        self.assertGreaterEqual(len(entries), 6)

        orig = engine._write_blob_to_path
        seen = []

        def fake(repo, spec, rel, mode):
            seen.append(rel)
            if len(seen) == 3:          # interrupt on the 3rd file
                raise KeyboardInterrupt
            return orig(repo, spec, rel, mode)

        engine._write_blob_to_path = fake
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                with self.assertRaises(KeyboardInterrupt):
                    engine._restore_entries(self.repo, entries, no_backup=True)
        finally:
            engine._write_blob_to_path = orig

        msg = buf.getvalue()
        self.assertIn("interrupted", msg)
        # Two files restored before the 3rd raised.
        self.assertIn("restored 2 of", msg)

    def test_lock_released_after_interrupt(self):
        # After an interrupted restore, a normal restore still succeeds -- the
        # lock was released as the KeyboardInterrupt unwound the context manager.
        ref = engine.resolve_ref(self.snap.id)
        entries = engine._paths_under(self.repo, ref, "")
        orig = engine._write_blob_to_path
        seen = []

        def fake(repo, spec, rel, mode):
            seen.append(rel)
            if len(seen) == 2:
                raise KeyboardInterrupt
            return orig(repo, spec, rel, mode)

        engine._write_blob_to_path = fake
        try:
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    engine._restore_entries(self.repo, entries, no_backup=True)
        finally:
            engine._write_blob_to_path = orig

        status, msg = engine.restore_all(self.repo, self.snap.id, no_backup=True)
        self.assertEqual(status, 0, msg)
        self.assertEqual(helpers.read_file(self.repo, "d/f5.txt"), b"v1-5")

if __name__ == "__main__":
    unittest.main()
