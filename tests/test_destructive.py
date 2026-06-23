#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Head-verb destructive detection."""
import os
import re
import unittest

from tests import helpers
from git_safepoint import destructive
from git_safepoint.destructive import looks_destructive


class DestructiveTrueCases(unittest.TestCase):
    def test_rm(self):
        self.assertTrue(looks_destructive("rm -rf notes"))

    def test_rmdir(self):
        self.assertTrue(looks_destructive("rmdir build"))

    def test_mv(self):
        self.assertTrue(looks_destructive("mv a b"))

    def test_git_checkout(self):
        self.assertTrue(looks_destructive("git checkout -- file.txt"))

    def test_git_reset_hard(self):
        self.assertTrue(looks_destructive("git reset --hard HEAD~1"))

    def test_git_clean(self):
        self.assertTrue(looks_destructive("git clean -fdx"))

    def test_git_with_global_flag(self):
        self.assertTrue(looks_destructive("git -C /tmp/repo restore file"))

    def test_truncating_redirect(self):
        self.assertTrue(looks_destructive("echo done > out.txt"))

    def test_redirect_pipe_truncate(self):
        self.assertTrue(looks_destructive("cat x >| out.txt"))

    def test_destructive_in_pipeline_tail(self):
        self.assertTrue(looks_destructive("cat list | xargs rm"))

    def test_destructive_after_and(self):
        self.assertTrue(looks_destructive("make && rm -rf dist"))

    def test_sudo_wrapper(self):
        self.assertTrue(looks_destructive("sudo rm -rf /var/tmp/x"))

    def test_env_assignment_prefix(self):
        self.assertTrue(looks_destructive("FOO=bar rm file"))

    def test_dd(self):
        self.assertTrue(looks_destructive("dd if=/dev/zero of=disk.img"))

    def test_shred(self):
        self.assertTrue(looks_destructive("shred -u secret"))


class DestructiveFalseCases(unittest.TestCase):
    """The spike's substring matcher mis-fired on all of these."""

    def test_grep_for_rm(self):
        self.assertFalse(looks_destructive("grep -r rm src/"))

    def test_find_named_rmdir(self):
        self.assertFalse(looks_destructive("find . -name rmdir"))

    def test_append_redirect_is_not_destructive(self):
        self.assertFalse(looks_destructive("echo done >> log.txt"))

    def test_ls(self):
        self.assertFalse(looks_destructive("ls -la"))

    def test_cat(self):
        self.assertFalse(looks_destructive("cat README.md"))

    def test_git_status(self):
        self.assertFalse(looks_destructive("git status"))

    def test_git_log(self):
        self.assertFalse(looks_destructive("git log --oneline"))

    def test_echo_with_rm_word(self):
        self.assertFalse(looks_destructive("echo please do not rm"))

    def test_empty(self):
        self.assertFalse(looks_destructive(""))

    def test_whitespace(self):
        self.assertFalse(looks_destructive("   "))

    def test_redirect_inside_single_quotes(self):
        self.assertFalse(looks_destructive("echo 'a > b'"))


class DestructiveOperatorGluedAndMultiline(unittest.TestCase):
    """Review C1/C2/H12/M4: separators glued to verbs, newlines, new git verbs.

    These all returned False under the old exact-token / first-segment logic and
    are the real-world forms agents emit; they MUST fire now.
    """

    def test_semicolon_glued(self):
        self.assertTrue(looks_destructive("echo hi;rm -rf x"))

    def test_semicolon_spaced(self):
        self.assertTrue(looks_destructive("echo hi; rm -rf x"))

    def test_and_glued(self):
        self.assertTrue(looks_destructive("cd foo&&rm -rf x"))

    def test_background_glued(self):
        self.assertTrue(looks_destructive("cd foo&rm -rf x"))

    def test_newline_separated_script(self):
        self.assertTrue(looks_destructive("set -e\ncd build\nrm -rf *"))

    def test_newline_second_command(self):
        self.assertTrue(looks_destructive("echo hi\nrm -rf notes"))

    def test_pipe_glued_to_rm(self):
        self.assertTrue(looks_destructive("echo x|rm"))

    def test_git_switch_force(self):
        self.assertTrue(looks_destructive("git switch -f other"))

    def test_git_branch_delete(self):
        self.assertTrue(looks_destructive("git branch -D feature"))

    def test_git_restore_trailing_subcommand(self):
        # `git restore` with the subcommand as the last token (no trailing space).
        self.assertTrue(looks_destructive("cd src && git restore ."))

    def test_unbalanced_quote_errs_toward_firing(self):
        # H12: an unparseable command is exactly when the safety net should fire.
        self.assertTrue(looks_destructive('echo "hi; rm -rf x'))


class DestructiveRedirectFalsePositives(unittest.TestCase):
    """Review M3: fd-dups and /dev/null redirects must NOT be flagged."""

    def test_fd_dup_stderr_to_stdout(self):
        # `2>&1` is an fd-dup, not a truncating file redirect. (Tail is `cat`,
        # not `tee log`: `tee` now correctly fires as a file-overwriting verb,
        # so it would no longer isolate the fd-dup behaviour here.)
        self.assertFalse(looks_destructive("make 2>&1 | cat"))

    def test_stderr_to_dev_null(self):
        self.assertFalse(looks_destructive("grep foo file 2>/dev/null"))

    def test_dup_to_stderr(self):
        self.assertFalse(looks_destructive("echo hi >&2"))

    def test_git_branch_list_not_destructive(self):
        self.assertFalse(looks_destructive("git branch -a"))


class DestructiveKnownGaps(unittest.TestCase):
    """Documented misses of the head-verb allowlist.

    These are NOT detected. The allowlist trades these misses for a near-zero
    false-positive rate; the README "既知の取りこぼし" section states the same
    honestly. Pinning them as tests keeps the gap visible and guards against a
    silent behaviour change (a future fix would flip these to assertTrue).
    """

    def test_find_exec_rm_is_missed(self):
        # head verb is `find`; the `-exec rm {} ;` argument is not parsed.
        self.assertFalse(looks_destructive("find . -name foo -exec rm {} ;"))

    def test_command_substitution_rm_is_missed(self):
        self.assertFalse(looks_destructive("echo $(rm -rf x)"))

    def test_backtick_substitution_rm_is_missed(self):
        self.assertFalse(looks_destructive("echo `rm -rf x`"))

    def test_subshell_rm_is_missed(self):
        # head token is the literal "(", not a destructive verb.
        self.assertFalse(looks_destructive("(rm -rf x)"))


class DestructiveDetectionR3Test(unittest.TestCase):
    """gzip/bzip2/xz/patch verbs + `>& file` truncation detection."""

    def test_new_verbs_fire(self):
        for c in ("gzip data.tsv", "bzip2 f", "xz big.log", "patch < p.diff",
                  "make && gzip x"):
            self.assertTrue(looks_destructive(c), c)

    def test_redirect_both_streams_to_filename_fires(self):
        self.assertTrue(looks_destructive("build >& out.txt"))

    def test_fd_dup_and_append_and_readonly_do_not_fire(self):
        for c in ("echo hi 2>&1", "echo hi >&2", "cat a >> log",
                  "grep -r xz src/", "echo ok > /dev/null"):
            self.assertFalse(looks_destructive(c), c)


class TeeFalsePositiveTest(unittest.TestCase):
    """N3: bare `tee` / `tee /dev/null` must NOT fire; `tee file` still does."""

    def test_tee_without_real_target_does_not_fire(self):
        for c in ("ls | tee", "echo hi | tee /dev/null", "cat x | tee -a log"):
            self.assertFalse(looks_destructive(c), c)

    def test_tee_with_file_target_fires(self):
        for c in ("echo hi | tee out.txt", "cmd | tee a/b.log"):
            self.assertTrue(looks_destructive(c), c)


class CompoundDestructiveTest(unittest.TestCase):
    def test_control_flow_bodies_fire(self):
        for c in (
            "if [ -d build ]; then rm -rf build; fi",
            "for f in *.tmp; do rm -f \"$f\"; done",
            "while read f; do rm $f; done",
            "{ rm -rf x; }",
            "! rm -rf x",
            "if true; then sudo rm -rf /opt/x; fi",  # reserved word + wrapper
        ):
            self.assertTrue(looks_destructive(c), c)

    def test_non_destructive_compound_does_not_fire(self):
        for c in (
            "if [ -d build ]; then echo build; fi",
            "for f in *.tmp; do cat \"$f\"; done",
            "{ echo hi; }",
        ):
            self.assertFalse(looks_destructive(c), c)


# --- secret backup / swap derivatives --------------------------------


class DestructiveWrapperValue(unittest.TestCase):
    """H5/M12: wrapper-value forms, in-place editors and tee."""

    def test_wrapper_value_forms(self):
        for cmd in ("sudo -u root rm -rf /tmp/x", "doas -u root rm -rf x",
                    "nice -n 10 rm -rf x", "timeout 5 rm -rf x",
                    "timeout -s KILL 30 rm -rf build", "ionice -c 3 rm -rf x"):
            self.assertTrue(looks_destructive(cmd), cmd)

    def test_inplace_editors(self):
        for cmd in ("sed -i s/a/b/ f.txt", "perl -i -pe s/a/b/ f.txt",
                    "awk -i inplace '{print}' f.txt", "sed -i.bak s/a/b/ f.txt"):
            self.assertTrue(looks_destructive(cmd), cmd)

    def test_tee_truncates_unless_append(self):
        self.assertTrue(looks_destructive("echo x | tee out.txt"))
        self.assertFalse(looks_destructive("echo x | tee -a out.txt"))
        self.assertFalse(looks_destructive("echo x | tee --append out.txt"))

    def test_wrapper_value_does_not_overshadow_safe_head(self):
        # `sudo -u root ls` is not destructive.
        self.assertFalse(looks_destructive("sudo -u root ls -la"))


class XargsAndWrappersRegression(unittest.TestCase):
    """R1: `xargs -i rm {}` must still be detected (the -i alias is glued)."""

    def test_xargs_i_does_not_swallow_command(self):
        for cmd in ("find . | xargs -i rm {}", "ls | xargs -i mv {} /tmp/",
                    "find . | xargs -i shred {}", "echo f | xargs -i truncate -s 0 {}"):
            self.assertTrue(looks_destructive(cmd), cmd)

    def test_xargs_I_separate_replace_still_detected(self):
        for cmd in ("find . | xargs -I {} rm {}", "find . | xargs -I{} rm -rf {}",
                    "ls | xargs -n 2 rm"):
            self.assertTrue(looks_destructive(cmd), cmd)


def _parse_zsh_array(text, name):
    """Parse a flat zsh array ``name=(a b c)`` (may be backslash-continued)."""
    m = re.search(r"\b" + re.escape(name) + r"=\((.*?)\)", text, re.S)
    assert m, "array {0} not found in adapter".format(name)
    body = m.group(1).replace("\\\n", " ").replace("\\", " ")
    return set(body.split())


def _parse_zsh_assoc(text, name):
    """Parse a zsh associative array ``name=(key "vals" key2 "vals2")``."""
    m = re.search(r"\b" + re.escape(name) + r"=\((.*?)\)", text, re.S)
    assert m, "assoc {0} not found in adapter".format(name)
    return {k: set(v.split()) for k, v in re.findall(r'(\S+)\s+"([^"]*)"', m.group(1))}


class ZshAdapterMirrorSyncTest(unittest.TestCase):
    """The zsh preexec adapter hand-mirrors destructive.py's detection tables
    (it cannot import the Python sets at shell-eval time). These tests fail if the
    two drift apart, so an edit to either side must update the other."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            helpers.PKG_ROOT, "adapters", "git-safepoint-preexec.zsh"
        )
        with open(path, encoding="utf-8") as fh:
            cls.zsh = fh.read()

    def test_verbs_in_sync(self):
        self.assertEqual(
            _parse_zsh_array(self.zsh, "_GSP_VERBS"),
            set(destructive.DESTRUCTIVE_VERBS),
        )

    def test_git_subcmds_in_sync(self):
        self.assertEqual(
            _parse_zsh_array(self.zsh, "_GSP_GIT_SUBCMDS"),
            set(destructive.DESTRUCTIVE_GIT_SUBCMDS),
        )

    def test_inplace_cmds_in_sync(self):
        self.assertEqual(
            _parse_zsh_array(self.zsh, "_GSP_INPLACE_CMDS"),
            set(destructive.DESTRUCTIVE_FLAG_CMDS),
        )

    def test_wrappers_in_sync(self):
        self.assertEqual(
            _parse_zsh_array(self.zsh, "_GSP_WRAPPERS"),
            set(destructive.WRAPPERS),
        )

    def test_wrapper_valopts_in_sync(self):
        self.assertEqual(
            _parse_zsh_assoc(self.zsh, "_GSP_WRAPPER_VALOPTS"),
            {k: set(v) for k, v in destructive._WRAPPER_VALUE_OPTS.items()},
        )


if __name__ == "__main__":
    unittest.main()
