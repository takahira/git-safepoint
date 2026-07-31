"""Snapshot labels must not carry credentials into the object store.

Both adapters put the triggering shell command into the label, and the label becomes
the commit subject -- i.e. it is persisted in refs/snapshots/* and printed by `list`.
secret.py only covers filenames, so without this the most common credential carrier
(the command line) went in verbatim.
"""
import unittest

from git_safepoint.engine import _make_message
from git_safepoint.labelsecret import MASK, redact_label


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
