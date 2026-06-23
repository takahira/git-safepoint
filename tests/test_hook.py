#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PreToolUse hook E2E: simulated tool_use JSON drives a pre-rm snapshot.

The real Claude Code session is not available offline; we feed the same JSON
shape on stdin (the documented contract) and prove the snapshot fires before a
destructive Bash, and does NOT fire for a read-only one. Then we recover.
"""
import io
import json
import os
import unittest
from contextlib import redirect_stderr

from tests import helpers
from git_safepoint import cli, engine

import argparse
import shutil
import stat
import subprocess
import sys
import tempfile
import time


def run_hook(event: dict, repo: str) -> int:
    """Invoke the `hook` subcommand with `event` on stdin; return exit code."""
    import sys

    raw = json.dumps(event)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(raw)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            rc = cli.main(["--repo", repo, "hook"])
    finally:
        sys.stdin = old_stdin
    return rc


class HookE2ETest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_destructive_bash_triggers_snapshot_and_recovers(self):
        # Untracked work that only the hook can save.
        helpers.write_file(self.repo, "notes/draft.md", "UNCOMMITTED untracked work")
        self.assertEqual(len(engine.list_refs(self.repo)), 0)

        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf notes"},
            "cwd": self.repo,
        }
        rc = run_hook(event, self.repo)
        self.assertEqual(rc, 0)  # fail-open: always allow
        self.assertEqual(len(engine.list_refs(self.repo)), 1)

        # The Bash command actually runs (simulated): delete the file.
        os.unlink(os.path.join(self.repo, "notes/draft.md"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "notes/draft.md")))

        # Recover from the snapshot the hook took.
        sid = engine.list_refs(self.repo)[0][0]
        status, _ = engine.restore_file(self.repo, sid, "notes/draft.md")
        self.assertEqual(status, 0)
        self.assertEqual(
            helpers.read_file(self.repo, "notes/draft.md"),
            b"UNCOMMITTED untracked work",
        )

    def test_non_destructive_bash_snapshots_on_first_call(self):
        # Conservative mode: every Bash call can snapshot. First call with no
        # prior snapshot creates one (no mtime cache to compare against).
        helpers.write_file(self.repo, "f.txt", "x")
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "cwd": self.repo,
        }
        rc = run_hook(event, self.repo)
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)

    def test_non_destructive_bash_no_duplicate_when_unchanged(self):
        # Second identical call with no file changes must not create a new snapshot.
        helpers.write_file(self.repo, "f.txt", "x")
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "cwd": self.repo,
        }
        run_hook(event, self.repo)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        run_hook(event, self.repo)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)

    def test_non_bash_tool_ignored(self):
        helpers.write_file(self.repo, "f.txt", "x")
        event = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/hosts"},
            "cwd": self.repo,
        }
        rc = run_hook(event, self.repo)
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 0)

    def test_bad_json_is_fail_open(self):
        import sys

        old_stdin = sys.stdin
        sys.stdin = io.StringIO("{ not json")
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = cli.main(["--repo", self.repo, "hook"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)  # never block on parse failure

    def test_hook_double_fire_collapses_via_tree_dedup(self):
        # A destructive Bash hook is force-rehash (debounce is BYPASSED), so a
        # repeated identical fire collapses at the tree-SHA dedup, not the
        # debounce gate. This pins the dedup path specifically (the old
        # name said "debounced" but force-rehash never reaches debounce).
        helpers.write_file(self.repo, "f.txt", "x")
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf f.txt"},
            "cwd": self.repo,
        }
        run_hook(event, self.repo)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        # Immediate identical fire, no change -> tree-SHA dedup (still 1).
        run_hook(event, self.repo)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)

    def test_debounce_skips_unchanged_manual_capture(self):
        # The DEBOUNCE gate proper (not force-rehash): a manual capture with
        # debounce>0 over an unchanged work tree skips with status 1 before any
        # tree is built (this path was previously uncovered).
        helpers.write_file(self.repo, "f.txt", "x")
        r1 = engine.capture(self.repo, via="manual", debounce=5.0)
        self.assertEqual(r1.status, 0)
        r2 = engine.capture(self.repo, via="manual", debounce=5.0)
        self.assertEqual(r2.status, 1)
        self.assertIn("debounce", r2.reason)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)

def _snap_blob(repo, snap_id, rel):
    ref = engine.resolve_ref(snap_id)
    return helpers.git(repo, "cat-file", "blob", "{0}:{1}".format(ref, rel)).stdout


def _run_hook(event):
    """Drive cli.main(['... hook']) with the event JSON on stdin."""
    from git_safepoint import cli

    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        return cli.main(["--repo", event.get("cwd", "."), "hook"])
    finally:
        sys.stdin = old


def _run_hook_raw(raw_json: str, repo: str) -> int:
    import sys

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(raw_json)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            return cli.main(["--repo", repo, "hook"])
    finally:
        sys.stdin = old_stdin


class HookFailOpenLeadingDashTest(unittest.TestCase):
    """The hook exits 0 even when the repo path begins with '-'."""

    def test_hook_exits_zero_when_project_dir_starts_with_dash(self):
        adapter = os.path.join(helpers.PKG_ROOT, "adapters", "pretooluse_hook.py")
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = "-x"  # would be mis-parsed as an argparse option
        proc = subprocess.run(
            [sys.executable, adapter],
            input=b'{"tool_name":"Bash","tool_input":{"command":"echo hi"},"cwd":"/tmp"}',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))


class HookPerRepoIsolationTest(unittest.TestCase):
    """One repo's capture exception must not starve later repos."""

    def test_failing_repo_does_not_block_the_other(self):
        repo_a = helpers.make_repo()
        repo_b = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo_a)
        self.addCleanup(helpers.cleanup, repo_b)
        helpers.write_file(repo_a, "a.txt", "a")
        helpers.write_file(repo_b, "b.txt", "b")
        real_a = os.path.realpath(repo_a)
        real_b = os.path.realpath(repo_b)

        seen = []
        orig = cli.engine.capture

        def flaky(repo, **kw):
            seen.append(os.path.realpath(repo))
            if os.path.realpath(repo) == real_a:
                raise RuntimeError("boom")
            return orig(repo, **kw)

        # A cross-repo destructive command touching both repos by absolute path.
        cmd = "rm -rf {0}/x {1}/y".format(real_a, real_b)
        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": real_a}
        )
        old_stdin = sys.stdin
        cli.engine.capture = flaky
        sys.stdin = io.StringIO(payload)
        try:
            rc = cli.cmd_hook(real_a, argparse.Namespace())
        finally:
            sys.stdin = old_stdin
            cli.engine.capture = orig

        self.assertEqual(rc, 0)  # fail-open
        self.assertIn(real_b, seen, "repo_b was never attempted after repo_a raised")
        self.assertTrue(engine.list_refs(repo_b), "repo_b got no protective snapshot")


class ReposFromCommandTest(unittest.TestCase):
    """_repos_from_command resolves relative nested repos and
    quoted absolute paths that contain a space."""

    @staticmethod
    def _init_repo(path):
        os.makedirs(path, exist_ok=True)
        helpers.git(path, "init", "-q")
        helpers.git(path, "config", "user.email", "t@e")
        helpers.git(path, "config", "user.name", "t")

    def test_relative_nested_repo_is_detected(self):
        outer = helpers.make_repo()
        self.addCleanup(helpers.cleanup, outer)
        nested = os.path.join(outer, "subrepo")
        self._init_repo(nested)
        helpers.write_file(nested, "work.txt", "uncommitted")
        # `rm -rf ./subrepo` run from the OUTER repo: the nested repo must be found
        # (the old absolute-only regex resolved "/subrepo" and missed it).
        repos = cli._repos_from_command("rm -rf ./subrepo", outer)
        real = {os.path.realpath(r) for r in repos}
        self.assertIn(os.path.realpath(nested), real)
        self.assertIn(os.path.realpath(outer), real)

    def test_quoted_absolute_path_with_space(self):
        base = tempfile.mkdtemp(prefix="git-safepoint-test-")
        self.addCleanup(helpers.cleanup, base)
        repo = os.path.join(base, "dir with space")
        self._init_repo(repo)
        # Quoted absolute path with a space -> one shlex token (the old regex split
        # it at the space and resolved the wrong / no repo).
        cmd = 'rm -rf "{0}/x"'.format(repo)
        repos = cli._repos_from_command(cmd, "/tmp")  # cwd intentionally unrelated
        self.assertIn(
            os.path.realpath(repo), {os.path.realpath(r) for r in repos}
        )

    def test_unbalanced_quotes_fall_back_without_crashing(self):
        # A tokenisation error must not raise; the cwd fallback still applies.
        repo = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        repos = cli._repos_from_command('rm -rf "oops', repo)
        self.assertIn(os.path.realpath(repo), {os.path.realpath(r) for r in repos})

    def test_bare_name_nested_repo_is_detected(self):
        # A bare name (no ./) that is an existing directory is walked,
        # so `rm -rf subrepo` still finds the nested repo.
        outer = helpers.make_repo()
        self.addCleanup(helpers.cleanup, outer)
        nested = os.path.join(outer, "subrepo")
        self._init_repo(nested)
        helpers.write_file(nested, "work.txt", "uncommitted")
        repos = cli._repos_from_command("rm -rf subrepo", outer)
        real = {os.path.realpath(r) for r in repos}
        self.assertIn(os.path.realpath(nested), real)
        self.assertIn(os.path.realpath(outer), real)

    def test_bare_name_nondir_arg_does_not_add_repos(self):
        # A bare-name FILE arg must NOT be walked (the stat gate skips it); only
        # cwd's repo is returned, via the fallback.
        outer = helpers.make_repo()
        self.addCleanup(helpers.cleanup, outer)
        helpers.write_file(outer, "notes.txt", "x")
        repos = cli._repos_from_command("rm -f notes.txt", outer)
        self.assertEqual(
            {os.path.realpath(r) for r in repos}, {os.path.realpath(outer)}
        )


class AdapterImportErrorFailOpenTest(unittest.TestCase):
    """N11: the hook adapter exits 0 even when the package cannot be imported
    (broken / partial install)."""

    def test_unimportable_package_still_exits_zero(self):
        src = os.path.join(helpers.PKG_ROOT, "adapters", "pretooluse_hook.py")
        tmp = tempfile.mkdtemp(prefix="git-safepoint-test-")
        self.addCleanup(helpers.cleanup, tmp)
        adir = os.path.join(tmp, "adapters")
        os.makedirs(adir)
        dst = os.path.join(adir, "pretooluse_hook.py")
        shutil.copyfile(src, dst)  # dirname(dirname(dst)) == tmp has no git_safepoint
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["CLAUDE_PROJECT_DIR"] = tmp
        proc = subprocess.run(
            [sys.executable, dst],
            input=b'{"tool_name":"Bash","tool_input":{"command":"echo hi"},"cwd":"/tmp"}',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=tmp,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))


@unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
class ZshPreexecTest(unittest.TestCase):
    """N13: smoke-test the zsh preexec destructive detector (runs on macOS / any
    runner with zsh; skipped otherwise)."""

    def _destructive(self, cmd):
        adapter = os.path.join(
            helpers.PKG_ROOT, "adapters", "git-safepoint-preexec.zsh"
        )
        proc = subprocess.run(
            ["zsh", "-c", 'source "$1"; _git_safepoint_is_destructive "$2"',
             "zsh", adapter, cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0

    def test_destructive_cases_fire(self):
        for c in ("rm -rf x", "git reset --hard", "gzip f", "foo > out",
                  "make && rm x", "build >& log"):
            self.assertTrue(self._destructive(c), c)

    def test_nondestructive_cases_do_not_fire(self):
        for c in ("ls -l", "echo hi", "cat a >> log", "git status",
                  "ls | tee", "echo hi 2>&1"):
            self.assertFalse(self._destructive(c), c)

    def test_dev_null_prefix_filenames_fire(self):
        # Data-loss-direction parity gap: a redirect TARGET that merely STARTS
        # WITH `/dev/null` is a REAL file, not the no-op device, so it MUST fire.
        # The old prefix-strip swallowed `> /dev/null` and missed the truncation;
        # the boundary-anchored token compare mirrors destructive.py's EXACT
        # `target not in {"/dev/null"}` check. Cross-checked vs Python below.
        for c in ("make > /dev/nullx", "make > /dev/null.bak"):
            self.assertTrue(self._destructive(c), c)

    def test_exact_dev_null_target_does_not_fire(self):
        # The exact no-op target `/dev/null` (spaced or contiguous, with/without a
        # trailing `2>&1`) must NOT fire -- the boundary anchor must not over-fire.
        for c in ("make > /dev/null", "make >/dev/null 2>&1"):
            self.assertFalse(self._destructive(c), c)

    def test_digit_leading_filename_redirect_fires(self):
        # `>& file` truncates BOTH streams into a file. A digit-LEADING but
        # non-numeric target (`>&2file`) is a FILENAME -> fires; a purely numeric
        # target (`2>&1` fd-dup) must NOT. Mirrors destructive.py's `>&` branch
        # (`target.isdigit()` -> fd-dup). The old `[^0-9]*` glob missed `>&2file`.
        self.assertTrue(self._destructive("cmd >&2file"), "cmd >&2file")
        self.assertFalse(self._destructive("cmd 2>&1"), "cmd 2>&1")

    def test_escaped_and_quoted_verbs_fire(self):
        # A backslash-escaped or quoted destructive verb must still
        # be detected -- zsh's ${(z)...} keeps the literal `\`/quote chars, so the
        # verb must be normalized to match (mirrors destructive.py posix-shlex).
        for c in (r"\rm -rf x", '"rm" -rf x', "'rm' -rf x"):
            self.assertTrue(self._destructive(c), c)

    def test_longform_wrapper_value_opts_fire(self):
        # Long-form wrapper value-options must consume their value so the
        # real verb (`rm`) surfaces -- e.g. `sudo --user root rm x` must not read
        # `root` as the head. Mirrors destructive.py _WRAPPER_VALUE_OPTS.
        for c in ("sudo --user root rm x", "nice --adjustment 10 rm x",
                  "env --unset FOO rm x", "timeout --signal TERM 5 rm x"):
            self.assertTrue(self._destructive(c), c)

    def test_spaced_dev_null_redirect_does_not_fire(self):
        # The idiomatic SPACED redirect to /dev/null must NOT be treated as
        # a truncating redirect (matches Python looks_destructive == False).
        for c in ("make > /dev/null 2>&1", "make >/dev/null 2>&1"):
            self.assertFalse(self._destructive(c), c)

    def test_redirect_to_real_file_still_fires(self):
        # Guard: a redirect to a real file MUST still fire.
        self.assertTrue(self._destructive("make > realout.txt"))

    def test_wrapper_and_compound_coverage_lock_in(self):
        # Lock in the divergence-prone wrapper / compound / flag branches.
        for c in ("sudo -u root rm x", "nice -n 10 rm x", "timeout 5 rm x",
                  "env rm x", "FOO=1 rm x", "git branch -D foo",
                  "sed -i s/a/b/ f", "tee out.txt", "if true; then rm x; fi"):
            self.assertTrue(self._destructive(c), c)


class HookNonStringFieldsTest(unittest.TestCase):
    """N6: a non-string cwd in the PreToolUse JSON falls back and still snapshots
    (rather than crashing into the skip-via-exception path)."""

    def test_non_string_cwd_falls_back_and_snapshots(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "x")
            payload = json.dumps({
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": 12345,  # non-string
            })
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(payload)
            try:
                rc = cli.cmd_hook(repo, argparse.Namespace())
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            # the --repo fallback repo was snapshotted despite the bad cwd
            self.assertTrue(engine.list_refs(repo))
        finally:
            helpers.cleanup(repo)


class ConservativeHookCapturesSwap(unittest.TestCase):
    """H3/M6: the real conservative=True hook path must protect the destructive
    moment and capture mode-only changes."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_conservative_captures_ctime_changed_swap(self):
        # A same mtime+size+inode+exec in-place content swap still
        # advances ctime (os.utime cannot roll ctime back), so the conservative
        # read-only hook now CAPTURES it instead of reusing the stale blob --
        # closing the read-only-path gap.
        p = helpers.write_file(self.repo, "f.txt", "AAAA")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        st = os.stat(p)
        time.sleep(0.01)  # let ctime advance even on coarse clocks
        with open(p, "wb") as fh:
            fh.write(b"BBBB")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime, not ctime
        st2 = os.stat(p)
        # Only ctime differs from the cached signature.
        self.assertEqual(st2.st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(st2.st_size, st.st_size)
        self.assertEqual(st2.st_ino, st.st_ino)
        self.assertNotEqual(st2.st_ctime_ns, st.st_ctime_ns)
        # Read-only Bash fires conservatively; the ctime change is now caught.
        _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls -la"},
                   "cwd": self.repo})
        rows = engine.list_refs(self.repo)
        self.assertEqual(len(rows), 2, "ctime-changed swap must be captured")
        self.assertEqual(_snap_blob(self.repo, rows[0][0], "f.txt"), b"BBBB")

    def test_conservative_captures_chmod_only_change(self):
        p = helpers.write_file(self.repo, "s.sh", "#!/bin/sh\necho hi\n")
        engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        os.chmod(p, 0o755)
        res = engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        self.assertEqual(res.status, 0)  # mode change was NOT skipped
        ref = engine.resolve_ref(engine.list_refs(self.repo)[0][0])
        mode, _sha = engine._path_mode_sha_in_ref(self.repo, ref, "s.sh")
        self.assertEqual(mode, "100755")


class HookFailOpen(unittest.TestCase):
    """M9/M11: stdin robustness and cross-repo coverage."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_none_stdin_fails_open(self):
        from git_safepoint import cli

        old = sys.stdin
        sys.stdin = None
        try:
            rc = cli.main(["--repo", self.repo, "hook"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)

    def test_bash_hook_snapshots_cross_repo_target(self):
        repo_b = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo_b)
        helpers.write_file(repo_b, "data.txt", "important uncommitted work")
        rc = _run_hook({
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf {0}/data.txt".format(repo_b)},
            "cwd": self.repo,
        })
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(repo_b)), 1)


class HookRepoResolutionRobustness(unittest.TestCase):
    """R5/R6/R7: env-scrubbed repo resolution, read-only fast path, notebook_path."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_leaked_git_dir_does_not_redirect_hook_snapshot(self):
        decoy = helpers.make_repo()
        self.addCleanup(helpers.cleanup, decoy)
        helpers.write_file(self.repo, "real.txt", "work")
        saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE")}
        os.environ["GIT_DIR"] = os.path.join(decoy, ".git")
        os.environ["GIT_WORK_TREE"] = decoy
        try:
            rc = _run_hook({
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf real.txt"},
                "cwd": self.repo,
            })
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        self.assertEqual(len(engine.list_refs(decoy)), 0)

    def test_readonly_bash_does_not_snapshot_other_repo(self):
        repo_b = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo_b)
        helpers.write_file(repo_b, "f.txt", "x")
        helpers.write_file(self.repo, "a.txt", "y")
        rc = _run_hook({
            "tool_name": "Bash",
            "tool_input": {"command": "cat {0}/f.txt".format(repo_b)},
            "cwd": self.repo,
        })
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(repo_b)), 0)

    def test_notebook_edit_uses_notebook_path(self):
        repo_b = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo_b)
        helpers.write_file(repo_b, "nb.ipynb", "{}")
        rc = _run_hook({
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": os.path.join(repo_b, "nb.ipynb")},
            "cwd": self.repo,
        })
        self.assertEqual(rc, 0)
        self.assertEqual(len(engine.list_refs(repo_b)), 1)


class HookContentSwapAndDedupTest(unittest.TestCase):
    """H4: capture the destructive-moment content swap; dedup identical re-fires."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_same_signature_content_swap_is_captured(self):
        p = os.path.join(self.repo, "f.txt")
        with open(p, "w") as fh:
            fh.write("AAAA")
        mtime = os.stat(p).st_mtime_ns
        engine.capture(self.repo, via="hook")
        self.assertEqual(len(engine.list_refs(self.repo)), 1)
        # Swap to identical size and pin the SAME mtime -> identical 3-pt signature
        # but different bytes. The signature debounce would miss this; force-rehash
        # at the hook moment must still capture it.
        with open(p, "w") as fh:
            fh.write("BBBB")
        os.utime(p, ns=(mtime, mtime))
        res = engine.capture(self.repo, via="hook")
        self.assertEqual(res.status, 0, res.reason)
        self.assertEqual(len(engine.list_refs(self.repo)), 2)

    def test_identical_refire_dedups(self):
        helpers.write_file(self.repo, "f.txt", "x")
        engine.capture(self.repo, via="hook")
        engine.capture(self.repo, via="hook")  # nothing changed
        self.assertEqual(len(engine.list_refs(self.repo)), 1)


class HookFailOpenTest(unittest.TestCase):
    """H1: the hook never crashes (always exit 0), even on odd JSON."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_non_object_json_exits_zero(self):
        for raw in ["[1, 2, 3]", "42", '"a string"', "null", "true"]:
            self.assertEqual(_run_hook_raw(raw, self.repo), 0, raw)

    def test_tool_input_not_a_dict_exits_zero(self):
        raw = json.dumps({"tool_name": "Bash", "tool_input": "rm -rf x"})
        self.assertEqual(_run_hook_raw(raw, self.repo), 0)

    def test_bad_json_exits_zero(self):
        self.assertEqual(_run_hook_raw("{not json", self.repo), 0)

if __name__ == "__main__":
    unittest.main()
