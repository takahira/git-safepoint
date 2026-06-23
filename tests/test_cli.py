#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI dispatch + exit codes + JSON list schema."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from tests import helpers
from git_safepoint import cli

import argparse
import subprocess
import sys

from git_safepoint import engine


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_snapshot_then_list_then_restore(self):
        helpers.write_file(self.repo, "f.txt", "hello")
        rc, out, _ = run_cli(["--repo", self.repo, "snapshot", "--label", "demo"])
        self.assertEqual(rc, 0)
        self.assertIn("snapshot ", out)

        rc, out, _ = run_cli(["--repo", self.repo, "list"])
        self.assertEqual(rc, 0)
        self.assertIn("files=1", out)

        rc, out, _ = run_cli(["--repo", self.repo, "list", "--json"])
        self.assertEqual(rc, 0)
        line = out.strip().splitlines()[0]
        meta = json.loads(line)
        for key in ("id", "ref", "commit", "files", "bytes", "label", "via", "epoch"):
            self.assertIn(key, meta)
        self.assertEqual(meta["files"], 1)
        self.assertEqual(meta["via"], "manual")

        sid = meta["id"]
        os.unlink(os.path.join(self.repo, "f.txt"))
        rc, out, _ = run_cli(["--repo", self.repo, "restore", sid, "f.txt"])
        self.assertEqual(rc, 0)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"hello")

    def test_non_repo_exit_two(self):
        d = tempfile.mkdtemp(prefix="not-a-repo-")
        try:
            rc, _, _ = run_cli(["--repo", d, "snapshot"])
            self.assertEqual(rc, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_empty_repo_snapshot_exit_one(self):
        rc, _, _ = run_cli(["--repo", self.repo, "snapshot"])
        self.assertEqual(rc, 1)

    def test_list_empty_exit_zero(self):
        rc, out, _ = run_cli(["--repo", self.repo, "list"])
        self.assertEqual(rc, 0)
        self.assertIn("no snapshots", out)

    def test_quiet_snapshot_no_stdout(self):
        helpers.write_file(self.repo, "f.txt", "x")
        rc, out, _ = run_cli(["--repo", self.repo, "snapshot", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_prune_cli(self):
        for i in range(4):
            helpers.write_file(self.repo, "f.txt", "v{0}".format(i))
            run_cli(["--repo", self.repo, "snapshot", "--force"])
        rc, out, _ = run_cli(
            ["--repo", self.repo, "prune", "--keep-generations", "2",
             "--max-bytes", "-1", "--keep-hours", "-1"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("pruned", out)

    def test_diff_cli(self):
        helpers.write_file(self.repo, "f.txt", "a\n")
        run_cli(["--repo", self.repo, "snapshot", "--force"])
        rc, out, err = run_cli(["--repo", self.repo, "list", "--json"])
        sid = json.loads(out.strip().splitlines()[0])["id"]
        helpers.write_file(self.repo, "f.txt", "b\n")
        rc, out, _ = run_cli(["--repo", self.repo, "diff", sid])
        self.assertEqual(rc, 0)
        self.assertIn("f.txt", out)

    def test_restore_interactive_non_tty_rejected(self):
        # `restore --interactive` is a TTY-only flow. In the test
        # harness stdin/stdout are redirected (not a TTY), so it must refuse
        # rather than blindly overwrite the work tree. Exit code 2.
        helpers.write_file(self.repo, "f.txt", "x")
        run_cli(["--repo", self.repo, "snapshot", "--force"])
        rc, _, err = run_cli(["--repo", self.repo, "restore", "--interactive"])
        self.assertEqual(rc, 2)
        self.assertIn("TTY", err)

    def test_restore_missing_id_rejected(self):
        # `restore` without an id and without --interactive is a usage error.
        rc, _, err = run_cli(["--repo", self.repo, "restore"])
        self.assertEqual(rc, 2)
        self.assertIn("id", err)


class MainCalledProcessErrorTest(unittest.TestCase):
    """An unexpected git failure exits cleanly, no traceback."""

    def test_called_process_error_returns_clean_status(self):
        repo = helpers.make_repo()
        try:
            orig = cli.cmd_list

            def boom(repo, args):
                raise subprocess.CalledProcessError(
                    1, ["git", "status"], output=b"", stderr=b"fatal: boom"
                )

            cli.cmd_list = boom
            try:
                rc = cli.main(["--repo", repo, "list"])
            finally:
                cli.cmd_list = orig
            self.assertEqual(rc, 2)
        finally:
            helpers.cleanup(repo)


class ListConcurrentPruneTest(unittest.TestCase):
    def test_list_skips_vanished_ref(self):
        repo = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        for i in range(3):
            helpers.write_file(repo, "f.txt", "v{0}".format(i))
            engine.capture(repo, force=True, debounce=0)
        rows = engine.list_refs(repo)
        self.assertEqual(len(rows), 3)
        victim = rows[1][0]

        orig = cli.engine.snapshot_meta

        def flaky(repo_, snap_id, ref):
            if snap_id == victim:
                raise subprocess.CalledProcessError(128, ["git", "rev-parse", ref])
            return orig(repo_, snap_id, ref)

        cli.engine.snapshot_meta = flaky
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            rc = cli.cmd_list(repo, argparse.Namespace(limit=0, json=True))
        finally:
            sys.stdout = old
            cli.engine.snapshot_meta = orig
        self.assertEqual(rc, 0)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2, "the surviving 2 refs are listed, victim skipped")


# --- R4 nits: #15 (in-repo symlink restore), #18 (scrub), #19 (limit) -------


class CliNonUtf8PrintTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _snap_ref_with_pem(self, snap_id, blob_bytes):
        """Build refs/snapshots/<snap_id> holding bad_\\xff_name.txt=blob_bytes."""
        name = b"bad_\xff_name.txt"
        blob = helpers.git(
            self.repo, "hash-object", "-w", "--stdin", input_bytes=blob_bytes
        ).stdout.decode("ascii").strip()
        rec = b"100644 " + blob.encode("ascii") + b"\t" + name + b"\x00"
        idx = os.path.join(self.repo, ".git", "tmp-idx-" + snap_id)
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
        helpers.git(
            self.repo, "update-ref", "refs/snapshots/" + snap_id, commit
        )
        os.unlink(idx)

    def test_cli_diff_nonutf8_path_no_crash_strict_stdout(self):
        # Two snapshots differing by a 0xff-byte path: `diff ID1 ID2` prints the
        # name-status to stdout. Pre-fix, under a strict-UTF-8 stdout this raised
        # UnicodeEncodeError at the print() site.
        self._snap_ref_with_pem("20260101-000000-0001-00001", b"v1\n")
        self._snap_ref_with_pem("20260101-000000-0002-00002", b"v2\n")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"  # strict stdout (the crashing locale)
        proc = subprocess.run(
            [sys.executable, "-m", "git_safepoint", "--repo", self.repo,
             "diff", "20260101-000000-0001-00001", "20260101-000000-0002-00002"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self.assertNotIn(b"UnicodeEncodeError", proc.stderr, proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ------------------------------------------------------ restore data-safety lows


class CliLowsTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_repos_from_command_finds_value_path_in_key_eq_value(self):
        other = helpers.make_repo()
        self.addCleanup(helpers.cleanup, other)
        target = os.path.join(other, "data")
        cmd = "dd if=/dev/zero of={0} bs=1M".format(target)
        repos = cli._repos_from_command(cmd, self.repo)
        # git rev-parse returns realpaths (macOS /var -> /private/var), so compare
        # resolved paths.
        resolved = {os.path.realpath(r) for r in repos}
        self.assertIn(
            os.path.realpath(other), resolved,
            "of=/abs value path must be detected (#5)",
        )

    def test_restore_all_without_yes_noninteractive_returns_2(self):
        helpers.write_file(self.repo, "f.txt", "x")
        res = engine.capture(self.repo, via="manual", force=True)
        # non-interactive (no TTY in the test runner), no --yes -> usage error = 2
        rc = cli.main(["--repo", self.repo, "restore", res.id, "--all"])
        self.assertEqual(rc, 2, "missing --yes is a usage error -> exit 2 (#10)")


# ------------------------------------------------------------- secret #8 / meta


class SnapshotMetaEpochTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_epoch_prefers_committer_date_over_legacy_id(self):
        helpers.write_file(self.repo, "f.txt", "x")
        res = engine.capture(self.repo, via="manual", force=True)
        ref = engine.resolve_ref(res.id)
        meta = engine.snapshot_meta(self.repo, res.id, ref)
        ct = int(
            helpers.git(self.repo, "log", "-1", "--format=%ct", ref)
            .stdout.decode().strip()
        )
        self.assertEqual(meta["epoch"], ct, "epoch must be the committer date (#25)")


class LabelTruncationEllipsisTest(unittest.TestCase):
    def test_make_message_marks_truncation_with_ellipsis(self):
        long_label = "x" * 200
        msg = engine._make_message("20260101-000000-0001-00001", long_label, "manual")
        # The label inside (...) is bounded and ends with an ellipsis marker (#26).
        inside = msg[msg.find("(") + 1: msg.rfind(")")]
        self.assertLessEqual(len(inside), engine.LABEL_MAX)
        self.assertTrue(inside.endswith("…"), inside)
        # A short label is untouched (no ellipsis).
        short = engine._make_message("20260101-000000-0001-00001", "tiny", "manual")
        self.assertIn("(tiny)", short)


class LabelDedupHintTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_dedup_with_label_surfaces_force_hint(self):
        helpers.write_file(self.repo, "f.txt", "x")
        engine.capture(self.repo, via="manual", force=True)  # first snapshot
        # Same tree + a label -> dedup drops the label; the reason must say how to
        # force-record it (#24).
        res = engine.capture(self.repo, via="manual", label="bookmark", debounce=0)
        self.assertEqual(res.status, 1)
        self.assertIn("--force", res.reason)


# ------------------------------------------------------------ restore #20 tests


class ListRefsOrdering(unittest.TestCase):
    """M8: same-date ties break by the monotonic seq, not PID/lexical."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_same_committerdate_breaks_by_seq(self):
        tree = helpers.git(self.repo, "write-tree").stdout.decode().strip()
        date = "2026-01-01T00:00:00"
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date

        def mk(seq):
            commit = subprocess.run(
                ["git", "-C", self.repo, "commit-tree", tree, "-m", str(seq)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True,
            ).stdout.decode().strip()
            sid = "20260101-000000-{0:04d}-1".format(seq)
            helpers.git(self.repo, "update-ref", engine.SNAP_REF_PREFIX + sid,
                        commit)
            return sid

        low = mk(1)
        high = mk(2)
        rows = engine.list_refs(self.repo)
        self.assertEqual(rows[0][0], high)
        self.assertEqual(rows[1][0], low)

if __name__ == "__main__":
    unittest.main()
