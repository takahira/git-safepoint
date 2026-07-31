"""Snapshot labels must not carry credentials into the object store.

Both adapters put the triggering shell command into the label, and the label becomes
the commit subject -- i.e. it is persisted in refs/snapshots/* and printed by `list`.
secret.py only covers filenames, so without this the most common credential carrier
(the command line) went in verbatim.
"""
import time
import unittest

from git_safepoint.engine import _make_message
from git_safepoint.labelsecret import _REDACT_BOUND, MASK, redact_label


class TestRedactLabel(unittest.TestCase):
    def test_credentials_are_masked_and_context_survives(self):
        cases = [
            ("export GITHUB_TOKEN=ghp_FAKEabc123456789", "ghp_FAKEabc123456789",
             ["GITHUB_TOKEN"]),
            ("mysql -uroot -pHunter2Passwd db", "Hunter2Passwd",
             ["mysql", "-uroot", "db"]),
            ("redis-cli -h 10.0.0.1 -a Sup3rSecret ping", "Sup3rSecret",
             ["redis-cli", "10.0.0.1", "ping"]),
            ('curl -H "Authorization: Bearer sk-live-FAKE123SECRET" https://api.example.com',
             "sk-live-FAKE123SECRET", ["Authorization", "Bearer", "api.example.com"]),
            ("psql postgres://user:s3cr3tpw@host:5432/db", "s3cr3tpw",
             ["postgres://user", "host:5432/db"]),
            ("tool --password sekret --port 1", "sekret", ["--password", "--port 1"]),
            ("sshpass -pMyPass ssh user@host", "MyPass", ["sshpass", "ssh user@host"]),
            ("deploy with AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", ["deploy with"]),
            ("run xoxb-1234567890-abcdefg now", "xoxb-1234567890-abcdefg", ["run", "now"]),
        ]
        for text, leaked, keep in cases:
            out = redact_label(text)
            self.assertNotIn(leaked, out, text)
            self.assertIn(MASK, out, text)
            for token in keep:
                self.assertIn(token, out, "{0!r} lost {1!r} -> {2!r}".format(text, token, out))

    def test_ordinary_commands_are_untouched(self):
        for text in (
            "pre-bash: git status",
            "docker run -p 8080:80 nginx",
            "psql -p 5432 mydb",
            "before refactor",
            "npm run build -- --mode production",
        ):
            self.assertEqual(redact_label(text), text)

    def test_masking_happens_before_truncation(self):
        # A credential must not survive merely by sitting inside the kept prefix.
        msg = _make_message("20260101-000000-0001-00001",
                            "pre-bash: export GITHUB_TOKEN=ghp_FAKEabc123456789", "hook")
        self.assertNotIn("ghp_FAKEabc123456789", msg)

    def test_message_without_label_is_unchanged(self):
        msg = _make_message("20260101-000000-0001-00001", None, "manual")
        self.assertEqual(msg, "snapshot 20260101-000000-0001-00001 via=manual")


class TestRedactLabelIsBounded(unittest.TestCase):
    """The pre-Bash hook passes the WHOLE command as the label, so redact_label
    must stay fast on huge input -- a stalled hook means NO protective snapshot
    before a destructive command, defeating the tool's whole purpose.
    """

    def test_16kb_alphanumeric_label_redacts_fast(self):
        # A long unbroken alphanumeric run is the worst case for the (formerly
        # unbounded) _KV_RE / _URL_CRED_RE leading runs: ~7s before the fix.
        label = "pre-bash: " + "a" * 16000
        start = time.perf_counter()
        out = redact_label(label)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, "16KB label took {0:.3f}s".format(elapsed))
        self.assertTrue(out.startswith("pre-bash: aaaa"))

    def test_multi_megabyte_label_redacts_fast(self):
        # Even linear regex passes over megabytes would stall the hook; the input
        # cap keeps the pass O(_REDACT_BOUND) regardless of command size.
        label = "pre-bash: " + ("echo hello && " * 150000)
        start = time.perf_counter()
        redact_label(label)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, "2MB label took {0:.3f}s".format(elapsed))

    def test_secret_in_head_of_huge_label_is_still_masked(self):
        # Capping the input must not weaken masking of what actually gets stored.
        label = "export GITHUB_TOKEN=ghp_FAKEabc123456789 && " + "x" * 50000
        out = redact_label(label)
        self.assertNotIn("ghp_FAKEabc123456789", out)
        self.assertIn(MASK, out)

    def test_secret_straddling_label_max_is_masked_whole(self):
        # LABEL_MAX is 60; start the token just before it so it straddles the
        # stored-prefix boundary. Redaction runs over the capped window (far wider
        # than LABEL_MAX), so the mask covers the token as a whole.
        label = "x" * 55 + " ghp_FAKEabc123456789 " + "y" * 20000
        msg = _make_message("20260101-000000-0001-00001", label, "hook")
        self.assertNotIn("ghp_FAKE", msg)

    def test_pem_sheared_by_the_cap_is_masked(self):
        # A PEM whose END marker lies beyond the cap must not leak key body via
        # the "no END, no match" escape hatch of the paired-marker rule.
        pem = "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * (_REDACT_BOUND * 2)
        out = redact_label("deploy: " + pem)
        self.assertNotIn("AAAA", out)
        self.assertIn(MASK, out)

    def test_short_unterminated_pem_marker_stays_visible(self):
        # The open-ended PEM rule applies only to capped input: a stray BEGIN
        # marker in a normal-sized label is not credential material.
        label = "grep -----BEGIN RSA PRIVATE KEY----- notes.txt"
        self.assertEqual(redact_label(label), label)


class TestCertificatesAreNotTreatedAsSecrets(unittest.TestCase):
    """Public certificate material must stay CAPTURED.

    secret.py states that dropping an untracked file is a protection gap, so
    excluding public material costs coverage for no secrecy gain.
    """

    def test_public_cert_formats_are_captured(self):
        from git_safepoint.secret import is_secret
        for name in ("server.crt", "ca.cer", "cert.der", "bundle.p7b", "req.csr"):
            self.assertFalse(is_secret(name), name)

    def test_ambiguous_and_private_formats_stay_excluded(self):
        from git_safepoint.secret import is_secret
        for name in ("server.pem", "id_rsa", ".env", "key.p12", "secret.asc"):
            self.assertTrue(is_secret(name), name)


if __name__ == "__main__":
    unittest.main()
