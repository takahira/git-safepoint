#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Opt-in capture of .gitignore'd paths, with the always-on secret floor.

Pins the two-stage design (GATE remaining_gate 1 / moat reconciliation):

1. By default ``.gitignore``'d paths are still excluded (secret-safe posture).
2. ``--include-ignored <pattern>`` rescues explicitly-named ignored artifacts
   (the copilot-cli #1675 ``output/`` shape).
3. Even under opt-in, secret patterns (``.env`` / ``*.key`` / ``*.pem`` /
   ``id_rsa`` ...) are NEVER captured.

Both directions are asserted: opt-in rescues ``output/``, and secrets are not
captured even when they sit inside an opted-in directory.
"""
import os
import unittest

from tests import helpers
from git_safepoint import engine, gitutil, secret


from git_safepoint import cli


def _seed_1675(repo):
    """Reproduce the copilot-cli #1675 corpus inside a fresh repo."""
    helpers.write_file(repo, "tracked.txt", "tracked v1\n")
    helpers.git(repo, "add", "tracked.txt")
    helpers.git(repo, "commit", "-qm", "base")
    # untracked, NOT ignored
    helpers.write_file(repo, "notes_untracked.txt", "human note\n")
    # .gitignore + ignored artifacts
    helpers.write_file(repo, ".gitignore", "output/\n*.log\n")
    helpers.write_file(repo, "output/data.jsonl", '{"row": 1}\n')
    helpers.write_file(repo, "debug.log", "logline\n")
    # secrets that happen to live under the ignored dir + a top-level one
    helpers.write_file(repo, "output/.env", "DB_PASSWORD=hunter2\n")
    helpers.write_file(repo, "output/server.pem", "-----BEGIN KEY-----\n")
    helpers.write_file(repo, ".env", "TOPLEVEL_SECRET=zzz\n")


def _names_in(repo, ref):
    return helpers.git(repo, "ls-tree", "-r", "--name-only", ref).stdout.decode()


class IncludeIgnoredOptInTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()
        _seed_1675(self.repo)

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_default_excludes_ignored(self):
        """No opt-in: ignored output/ + *.log are still excluded (status quo)."""
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0)
        names = _names_in(self.repo, engine.resolve_ref(res.id))
        self.assertIn("notes_untracked.txt", names)
        self.assertNotIn("output/data.jsonl", names)
        self.assertNotIn("debug.log", names)

    def test_optin_rescues_output_but_not_secrets(self):
        """opt-in output/ rescues data.jsonl yet still drops .env / .pem.

        This is the core moat assertion: ``--include-ignored output/`` saves
        the copilot-cli #1675 artifact (PASS) while the secret floor keeps
        ``output/.env`` and ``output/server.pem`` out of the object store.
        """
        res = engine.capture(
            self.repo, via="manual", force=True, include_ignored=["output/"]
        )
        self.assertEqual(res.status, 0)
        names = _names_in(self.repo, engine.resolve_ref(res.id))
        # rescued
        self.assertIn("output/data.jsonl", names)
        # secrets NEVER captured, even inside the opted-in dir
        self.assertNotIn("output/.env", names)
        self.assertNotIn("output/server.pem", names)
        # a non-opted-in ignored glob is still excluded
        self.assertNotIn("debug.log", names)

    def test_optin_rescued_file_restores_after_clean(self):
        """End-to-end: snapshot output/ via opt-in, git clean -fdx, restore."""
        res = engine.capture(
            self.repo, via="manual", force=True, include_ignored=["output/"]
        )
        self.assertEqual(res.status, 0)
        # the #1675 disaster: clean -fdx wipes untracked + ignored
        helpers.git(self.repo, "clean", "-fdx")
        self.assertFalseExists("output/data.jsonl")
        status, _ = engine.restore_file(self.repo, res.id, "output/data.jsonl")
        self.assertEqual(status, 0)
        self.assertEqual(
            helpers.read_file(self.repo, "output/data.jsonl"), b'{"row": 1}\n'
        )

    def test_secret_floor_applies_without_optin(self):
        """A top-level untracked .env is never captured even with no opt-in."""
        res = engine.capture(self.repo, via="manual", force=True)
        self.assertEqual(res.status, 0)
        names = _names_in(self.repo, engine.resolve_ref(res.id))
        self.assertNotIn(".env", names.split("\n"))

    def test_log_glob_optin_rescues_log_only(self):
        """opt-in '*.log' rescues debug.log but not output/data.jsonl."""
        res = engine.capture(
            self.repo, via="manual", force=True, include_ignored=["*.log"]
        )
        self.assertEqual(res.status, 0)
        names = _names_in(self.repo, engine.resolve_ref(res.id))
        self.assertIn("debug.log", names)
        self.assertNotIn("output/data.jsonl", names)

    # helper assertion
    def assertFalseExists(self, rel):
        self.assertFalse(os.path.exists(os.path.join(self.repo, rel)))


class SecretMatcherUnitTest(unittest.TestCase):
    """Direct unit coverage of the secret matcher (path-based, not content)."""

    def test_known_secret_shapes_match(self):
        for rel in [
            ".env",
            ".env.local",
            "config/.env.production",
            "deploy/server.key",
            "certs/server.pem",
            "ssh/id_rsa",
            "ssh/id_ed25519",
            "vault.secret",
            ".netrc",
        ]:
            self.assertTrue(secret.is_secret(rel), "expected secret: " + rel)

    def test_non_secret_shapes_do_not_match(self):
        for rel in [
            "output/data.jsonl",
            "debug.log",
            "src/app.py",
            "notes_untracked.txt",
            "README.md",
            "environment.txt",
        ]:
            self.assertFalse(secret.is_secret(rel), "false positive: " + rel)

    def test_case_insensitive(self):
        self.assertTrue(secret.is_secret("Config/SERVER.PEM"))
        self.assertTrue(secret.is_secret(".ENV"))

    def test_drop_secrets_splits_and_preserves_order(self):
        kept, dropped = secret.drop_secrets(
            ["a.txt", ".env", "b/c.py", "k.key", "d.md"]
        )
        self.assertEqual(kept, ["a.txt", "b/c.py", "d.md"])
        self.assertEqual(dropped, [".env", "k.key"])


class ListWorktreeFilesTest(unittest.TestCase):
    """gitutil.list_worktree_files returns (files, dropped_secrets)."""

    def setUp(self):
        self.repo = helpers.make_repo()
        _seed_1675(self.repo)

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_returns_dropped_secrets(self):
        files, dropped = gitutil.list_worktree_files(
            self.repo, include_ignored=["output/"]
        )
        self.assertIn("output/data.jsonl", files)
        self.assertNotIn("output/.env", files)
        self.assertIn("output/.env", dropped)
        self.assertIn("output/server.pem", dropped)
        # top-level untracked .env is also a dropped secret
        self.assertIn(".env", dropped)


class PreexecIncludeIgnoredEnvTest(unittest.TestCase):
    """The snapshot subcommand (preexec path) honours the opt-in env."""

    _KEYS = ("GIT_SAFEPOINT_INCLUDE_IGNORED",)

    def setUp(self):
        self.repo = helpers.make_repo()
        self._saved = {k: os.environ.pop(k, None) for k in self._KEYS}
        helpers.write_file(self.repo, "a.txt", "a")
        helpers.git(self.repo, "add", "a.txt")
        helpers.git(self.repo, "commit", "-qm", "init")
        helpers.write_file(self.repo, ".gitignore", "output/\n")
        helpers.git(self.repo, "add", ".gitignore")
        helpers.git(self.repo, "commit", "-qm", "ignore")
        helpers.write_file(self.repo, "output/data.bin", "ARTIFACT")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        helpers.cleanup(self.repo)

    def _snap_paths(self):
        ref = engine.resolve_ref(engine.list_refs(self.repo)[0][0])
        return [rel for _m, _s, rel in engine._paths_under(self.repo, ref, "")]

    def test_snapshot_subcommand_honours_env(self):
        os.environ["GIT_SAFEPOINT_INCLUDE_IGNORED"] = "output/"
        rc = cli.main(["--repo", self.repo, "snapshot", "--via", "preexec", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertIn("output/data.bin", self._snap_paths())

    def test_snapshot_subcommand_without_env_excludes_ignored(self):
        # Control: no env, no flag -> the ignored artifact stays out (default).
        rc = cli.main(["--repo", self.repo, "snapshot", "--via", "preexec", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertNotIn("output/data.bin", self._snap_paths())


class IncludeIgnoredEnvTest(unittest.TestCase):
    """The opt-in env var name for capturing ignored artifacts."""

    _KEYS = ("GIT_SAFEPOINT_INCLUDE_IGNORED",)

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self._KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_new_name(self):
        os.environ["GIT_SAFEPOINT_INCLUDE_IGNORED"] = "output/:dist/"
        self.assertEqual(cli._include_ignored_from_env(), ["output/", "dist/"])

    def test_unset_returns_none(self):
        self.assertIsNone(cli._include_ignored_from_env())

if __name__ == "__main__":
    unittest.main()
