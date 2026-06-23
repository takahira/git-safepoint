#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Secret detection & the always-on secret floor (secret.py).

Pins that secret-shaped paths (.env / *.key / *.pem / id_rsa ...) are
NEVER captured into a snapshot, never reach the object DB, never leak via
diff, and that the floor holds across backup derivatives and opt-in paths.
"""
from __future__ import annotations

import os
import unittest

from tests import helpers
from git_safepoint import engine, gitutil, secret


class SecretFloorR3Test(unittest.TestCase):
    """Expanded secret floor + nested slash-pattern matching."""

    def test_new_patterns_excluded(self):
        for n in (
            "myapp-firebase-adminsdk-abc-012.json",
            "secrets.json", "secrets.toml",
            ".flaskenv", "app.flaskenv",
            ".my.cnf", ".mylogin.cnf", "_netrc",
        ):
            self.assertTrue(secret.is_secret(n), n)

    def test_nested_slash_patterns_excluded_at_any_depth(self):
        for n in (
            ".kube/config", ".docker/config.json",
            "vendor/.kube/config", "deploy/k8s/.docker/config.json",
        ):
            self.assertTrue(secret.is_secret(n), n)

    def test_no_over_exclusion(self):
        # Legitimate non-secret paths must stay capturable.
        for n in (
            "src/config", "app/config.py", "my.kube/config",
            "notes/secrets.md", "data/output.json", "flaskenv.txt",
        ):
            self.assertFalse(secret.is_secret(n), n)

    def test_firebase_key_not_captured_into_object_db(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "app.py", "x")
            helpers.write_file(
                repo, "myapp-firebase-adminsdk-xy-01.json",
                '{"private_key":"-----BEGIN PRIVATE KEY-----"}',
            )
            res = engine.capture(repo, via="manual", force=True, debounce=0)
            self.assertEqual(res.status, 0, res.reason)
            ref = engine.resolve_ref(engine.list_refs(repo)[0][0])
            names = [rel for _m, _s, rel in engine._paths_under(repo, ref, "")]
            self.assertIn("app.py", names)
            self.assertNotIn("myapp-firebase-adminsdk-xy-01.json", names)
        finally:
            helpers.cleanup(repo)


class SecretBlobAbsentFromObjectDBTest(unittest.TestCase):
    """N10: a secret's blob is never written to .git/objects on the capture path
    (asserted at the OBJECT-DB level, not just the snapshot tree)."""

    def test_secret_blob_never_written(self):
        repo = helpers.make_repo()
        try:
            secret_bytes = b"SUPER-SECRET-TOKEN-do-not-store"
            helpers.write_file(repo, "app.py", "x")
            helpers.write_file(repo, ".env", secret_bytes)
            sha = helpers.git(
                repo, "hash-object", "--stdin", input_bytes=secret_bytes
            ).stdout.decode().strip()
            res = engine.capture(repo, via="manual", force=True, debounce=0)
            self.assertEqual(res.status, 0, res.reason)
            probe = helpers.git(repo, "cat-file", "-e", sha, check=False)
            self.assertNotEqual(
                probe.returncode, 0, "secret blob was written to the object DB"
            )
        finally:
            helpers.cleanup(repo)


class SecretBackupDerivativesTest(unittest.TestCase):
    def test_backup_and_swap_copies_are_secret(self):
        for n in (
            ".env~", "#.env#", "id_rsa.bak", "id_rsa~", "id_rsa.orig",
            "server.pem.bak", "server.pem~", "app.key.bak", "cert.crt.bak",
            ".netrc.bak", ".npmrc.bak", "database.yml.bak", "kubeconfig.bak",
            "id_ed25519.swp", "secrets/server.pem.save",
        ):
            self.assertTrue(secret.is_secret(n), n)

    def test_plain_backups_are_not_secret(self):
        # De-decoration must not turn ordinary backups into secrets.
        for n in ("notes.bak", "data.old", "main.py", "report.tmp",
                  "config.json.bak", "README.md~"):
            self.assertFalse(secret.is_secret(n), n)

    def test_capture_excludes_secret_backups(self):
        repo = helpers.make_repo()
        self.addCleanup(helpers.cleanup, repo)
        helpers.write_file(repo, "main.py", "print(1)\n")
        helpers.write_file(repo, ".env~", "AWS_SECRET_ACCESS_KEY=leak\n")
        helpers.write_file(repo, "id_rsa.bak", "-----BEGIN PRIVATE KEY-----\n")
        r = engine.capture(repo, force=True)
        self.assertEqual(r.status, 0, r.reason)
        rows = engine.list_refs(repo)
        names = {rel for _m, _s, rel in
                 engine._paths_under(repo, engine.resolve_ref(rows[0][0]), "")}
        self.assertIn("main.py", names)
        self.assertNotIn(".env~", names)
        self.assertNotIn("id_rsa.bak", names)


# --- leaked commit-date env is scrubbed ------------------------------


class H2SecretFloorTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_credentials_source_kept_payload_dropped(self):
        for src in ("credentials.py", "credentials.go", "app/credentials.ts"):
            self.assertFalse(secret.is_secret(src), src)
        for payload in (
            "credentials.json", "credentials.yaml", "credentials.yml",
            "credentials.xml", "credentials.ini", "credentials.toml",
            "credentials.cfg",
        ):
            self.assertTrue(secret.is_secret(payload), payload)

    def test_c12_new_floor_patterns(self):
        for name in (
            "id_ed25519_sk", "id_ecdsa_sk", ".envrc", "svc.keytab",
            "pgpass.conf", "krb/http.keytab",
        ):
            self.assertTrue(secret.is_secret(name), name)

    def test_tracked_secret_exempt_untracked_dropped(self):
        helpers.write_file(self.repo, "id_rsa", "PRIVATE-KEY")
        helpers.git(self.repo, "add", "id_rsa")
        helpers.git(self.repo, "commit", "-qm", "track key")
        helpers.write_file(self.repo, "id_dsa", "UNTRACKED-KEY")
        kept, dropped = gitutil.list_worktree_files(self.repo)
        self.assertIn("id_rsa", kept, "tracked secret is exempt (H2)")
        self.assertIn("id_dsa", dropped, "untracked secret still dropped")


# --------------------------------------------------------------------------- C2


class C11DiffSecretLeakTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_diff_does_not_hash_untracked_secret_via_old_snapshot_path(self):
        # A secret path that is in an OLD snapshot (captured while tracked, H2
        # exemption) but is now an UNTRACKED secret in the work tree must not have
        # its CURRENT plaintext hashed into .git/objects by the diff.
        helpers.write_file(self.repo, "server.pem", "OLD-PRIVATE")
        helpers.git(self.repo, "add", "server.pem")
        helpers.git(self.repo, "commit", "-qm", "track pem")
        res = engine.capture(self.repo, via="manual", force=True)  # snapshot has it
        helpers.git(self.repo, "rm", "--cached", "-q", "server.pem")  # untrack
        helpers.write_file(self.repo, "server.pem", "NEW-PRIVATE-SECRET")

        # blob id of the CURRENT secret content (computed WITHOUT -w).
        leak_sha = helpers.git(
            self.repo, "hash-object", "--stdin",
            input_bytes=b"NEW-PRIVATE-SECRET",
        ).stdout.decode().strip()

        status, _ = engine.diff_name_status(self.repo, res.id, None)
        self.assertEqual(status, 0)
        # The leaked blob must NOT be in the object DB.
        present = helpers.git(
            self.repo, "cat-file", "-e", leak_sha, check=False
        ).returncode == 0
        self.assertFalse(present, "untracked secret plaintext must not be hashed")


# --------------------------------------------------------------------------- #9


class SecretSlashBackupTest(unittest.TestCase):
    def test_slash_pattern_backup_decoration(self):
        for name in (
            ".kube/config.bak", ".kube/config~", ".kube/config.swp",
            ".docker/config.json.bak", "vendor/.kube/config.bak",
        ):
            self.assertTrue(secret.is_secret(name), name)
        # A bare config.json.bak WITHOUT the slash-prefixed floor context must NOT
        # match (keeps the scoping that an earlier review locked in).
        self.assertFalse(secret.is_secret("config.json.bak"))


class FloorR6AdditionsTest(unittest.TestCase):
    def test_new_floor_patterns(self):
        for name in (".s3cfg", ".boto", ".vault-token", "app/.s3cfg", ".s3cfg.bak"):
            self.assertTrue(secret.is_secret(name), name)


# ------------------------------------------------------------------------- G


class SecretFloor(unittest.TestCase):
    """H4/L4/L5: secret exclusion completeness."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_new_high_value_patterns(self):
        for name in (".git-credentials", "service-account.json", "release.jks",
                     "AuthKey.p8", ".npmrc", ".pypirc", "prod.tfstate",
                     "client.ovpn", "key.asc", "config.gpg"):
            self.assertTrue(secret.is_secret(name), name)

    def test_review_d_residual_patterns_excluded(self):
        # Names that slipped past the secret floor must now be secret.
        for name in (
            "aws_credentials", "git-credentials", "credentials.json",
            "credentials.yml", "client_secret_12345.json", "secrets.yaml",
            "secrets.yml", "prod.tfvars.json", "terraform.tfvars.json",
            "config/database.yml", "app.cer", "app.der", "bundle.p7b",
            "request.csr", ".htpasswd", "vault.kdbx",
            ".ssh/authorized_keys", "wp-config.php",
        ):
            self.assertTrue(secret.is_secret(name), name)

    def test_review_d_does_not_overmatch_source_dirs(self):
        # The basename-targeted globs must not drop a whole source tree that
        # merely contains an ambiguous word in a directory name (R2 concern).
        for name in (
            "src/credentials_helper/util.py",   # dir named *credentials*
            "app/secrets_manager/__init__.py",  # dir named secrets*
            "lib/client_secret_view.py",        # client_secret* but a .py source
        ):
            self.assertFalse(secret.is_secret(name), name)

    def test_secret_floor_wins_over_include_ignored_glob(self):
        helpers.write_file(self.repo, ".gitignore", "*\n")
        helpers.write_file(self.repo, "id_rsa", "PRIVATE")
        helpers.write_file(self.repo, "app.key", "secret")
        helpers.write_file(self.repo, "keep.txt", "ok")
        files, dropped = gitutil.list_worktree_files(
            self.repo, include_ignored=["*"]
        )
        self.assertIn("id_rsa", dropped)
        self.assertIn("app.key", dropped)
        self.assertNotIn("id_rsa", files)
        self.assertNotIn("app.key", files)
        self.assertIn("keep.txt", files)

    def test_secret_uppercase_glob_via_include_ignored(self):
        # case-insensitive floor: *.KEY is still a secret.
        self.assertTrue(secret.is_secret("APP.KEY"))

    def test_directory_named_dotfile_secret_excluded(self):
        # A directory literally named like a DOTFILE secret excludes its files.
        self.assertTrue(secret.is_secret(".env/config"))
        self.assertTrue(secret.is_secret(".netrc/whatever"))
        self.assertFalse(secret.is_secret("env/config"))  # 'env' != '.env'

    def test_common_source_dir_not_over_excluded(self):
        # Ambiguous English words must NOT exclude whole source trees.
        self.assertFalse(secret.is_secret("src/credentials/auth.py"))
        self.assertFalse(secret.is_secret("app/kubeconfig/setup.md"))
        self.assertFalse(secret.is_secret("pkg/id_rsa/reader.go"))
        # but the credential FILE itself is still a secret.
        self.assertTrue(secret.is_secret("credentials"))
        self.assertTrue(secret.is_secret("aws/credentials"))
        self.assertTrue(secret.is_secret("kubeconfig"))

    def test_trailing_whitespace_secret_excluded(self):
        self.assertTrue(secret.is_secret(".env "))
        self.assertTrue(secret.is_secret(".env\n.bak"))


class DiffNoSecretBlobWrite(unittest.TestCase):
    """R3 / Codex P1: the worktree diff must not write secret blobs nor list them,
    while still showing new non-secret files and deletions."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_diff_does_not_persist_secret_blob(self):
        helpers.write_file(self.repo, "tracked.txt", "v1\n")
        helpers.write_file(self.repo, "gone.txt", "bye\n")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        snap = engine.list_refs(self.repo)[0][0]
        # un-ignored secret + a brand-new normal file; delete a captured file.
        helpers.write_file(self.repo, ".env", "SECRET=topsecret\n")
        helpers.write_file(self.repo, "new.txt", "hello\n")
        os.remove(os.path.join(self.repo, "gone.txt"))
        secret_sha = helpers.git(
            self.repo, "hash-object", os.path.join(self.repo, ".env")
        ).stdout.decode().strip()
        status, text = engine.diff_name_status(self.repo, snap, None)
        self.assertEqual(status, 0)
        self.assertNotIn(".env", text)        # secret never shown
        self.assertIn("new.txt", text)        # new non-secret file shown (A)
        self.assertIn("gone.txt", text)       # deleted captured file shown (D)
        # the secret's blob must NOT have been written to the object DB
        probe = helpers.git(self.repo, "cat-file", "-e", secret_sha, check=False)
        self.assertNotEqual(probe.returncode, 0)

    def test_diff_file_absent_path_reports_not_in_snapshot(self):
        helpers.write_file(self.repo, "a.txt", "x\n")
        engine.capture(self.repo, via="manual", debounce=0, force=True)
        snap = engine.list_refs(self.repo)[0][0]
        status, text = engine.diff_file(self.repo, snap, "nonexistent.txt")
        self.assertEqual(status, 3)
        self.assertIn("not in snapshot", text)

if __name__ == "__main__":
    unittest.main()
