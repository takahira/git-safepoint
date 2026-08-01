#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Head-verb destructive detection."""
import os
import subprocess
import shutil
import shlex
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
    false-positive rate; the README "Known limitations" section states the same
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

    def test_cluster_flags_in_sync(self):
        self.assertEqual(
            _parse_zsh_assoc(self.zsh, "_GSP_CLUSTER_FLAGS"),
            {k: set(v) for k, v in destructive.DESTRUCTIVE_CLUSTER_FLAG_CMDS.items()},
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


class OverwriteClass(unittest.TestCase):
    """Issue #3: commands that REPLACE an existing destination.

    The zsh preexec path has no conservative fallback, so an unrecognised
    overwrite there is fully unprotected. README's motivating example is a `cp`
    overwrite, so these belong in the allowlist.
    """

    def test_cp_overwrite(self):
        self.assertTrue(looks_destructive("cp -f new.txt old.txt"))

    def test_cp_plain(self):
        # Fired on the verb without inspecting the destination: over-firing on a
        # copy to a fresh path is a harmless deduped snapshot.
        self.assertTrue(looks_destructive("cp a.txt b.txt"))

    def test_rsync_delete(self):
        self.assertTrue(looks_destructive("rsync -a --delete src/ dst/"))

    def test_install(self):
        self.assertTrue(looks_destructive("install -m 755 bin/tool /usr/local/bin/tool"))

    def test_unzip_overwrite_flag(self):
        self.assertTrue(looks_destructive("unzip -o bundle.zip"))

    def test_unzip_without_overwrite_flag(self):
        # A bare unzip prompts before replacing, so it is not a silent overwrite.
        self.assertFalse(looks_destructive("unzip bundle.zip"))

    def test_tar_extract_clustered(self):
        self.assertTrue(looks_destructive("tar -xzf archive.tar.gz"))

    def test_tar_extract_dashless(self):
        self.assertTrue(looks_destructive("tar xzf archive.tar.gz"))

    def test_tar_extract_long_option(self):
        self.assertTrue(looks_destructive("tar --extract --file archive.tar"))

    def test_tar_create_overwrites_an_existing_archive(self):
        # Regression for a wrong assumption in the first cut of this feature:
        # `tar -c` was treated as safe, but writing an archive TRUNCATES an
        # existing file of that name. Measured: a file holding real data was
        # replaced by gzip bytes, with no snapshot taken.
        self.assertTrue(looks_destructive("tar -czf archive.tar.gz src"))
        self.assertTrue(looks_destructive("tar czf archive.tar src"))

    def test_tar_list_with_x_in_filename_does_not_fire(self):
        # The dashless bundle is only read from the FIRST argument, so an
        # ordinary all-alpha filename containing the trigger letter is safe.
        self.assertFalse(looks_destructive("tar -tf box"))

    def test_attached_option_value_does_not_hide_the_trigger(self):
        # `-f` consumes the rest of the token, so the cluster is not all-alpha.
        # Requiring isalpha() made these genuine overwrites invisible.
        # `tar -xvfbackup.tar` was verified to really extract (BSD tar).
        self.assertTrue(looks_destructive("tar -xvfbackup.tar"))
        self.assertTrue(looks_destructive("tar -xC/tmp -farchive.tar"))
        self.assertTrue(looks_destructive("unzip -od. backup.zip"))
        self.assertTrue(looks_destructive("ln -ft/tmp source"))

    def test_trigger_letter_inside_an_attached_value_does_not_fire(self):
        # The other direction: in `-tvfxyz.tar` the x belongs to the FILE NAME
        # (f already consumed the remainder), so a plain listing must stay quiet.
        self.assertFalse(looks_destructive("tar -tvfxyz.tar"))
        self.assertFalse(looks_destructive("tar -tf a.tar"))

    def test_ln_force_clustered(self):
        self.assertTrue(looks_destructive("ln -sf target link"))

    def test_ln_without_force(self):
        self.assertFalse(looks_destructive("ln -s target link"))


if __name__ == "__main__":
    unittest.main()


class NoWriteModeTest(unittest.TestCase):
    """A provably-no-write invocation must not force a full-tree rehash, and a
    real one must still fire. The table is deliberately tiny: a false 'safe' here
    means NO snapshot before real data loss."""

    SAFE = ("rsync -avn src/ dst/", "rsync --dry-run src/ dst/",
            "rsync -n --delete src/ dst/",
            "tar -xOf archive.tar f", "tar xOf archive.tar f",
            "tar --to-stdout -xf archive.tar")
    FIRES = ("rsync -av src/ dst/", "rsync -av --delete src/ dst/",
             "tar -xf archive.tar", "tar -xvfnotes.tar",
             # a no-write mode does NOT excuse a truncating redirect
             "rsync -avn src/ dst/ > out.txt",
             "tar -xOf archive.tar f > out.txt")

    def test_no_write_modes_do_not_fire(self):
        for cmd in self.SAFE:
            self.assertFalse(destructive.looks_destructive(cmd), cmd)

    def test_real_invocations_still_fire(self):
        for cmd in self.FIRES:
            self.assertTrue(destructive.looks_destructive(cmd), cmd)

    def test_letter_inside_an_attached_value_is_not_a_mode(self):
        """`-xvfnotes.tar` has an 'O'-free name, but the scan must stop at `f`
        rather than reading later characters as flags in either direction."""
        self.assertTrue(destructive.looks_destructive("tar -xvfOut.tar"))

    def test_no_write_table_is_mirrored_in_the_zsh_adapter(self):
        path = os.path.join(helpers.PKG_ROOT, "adapters",
                            "git-safepoint-preexec.zsh")
        with open(path, encoding="utf-8") as fh:
            zsh = fh.read()
        long_opts = _parse_zsh_assoc(zsh, "_GSP_NO_WRITE_LONG")
        letters = _parse_zsh_assoc(zsh, "_GSP_NO_WRITE_LETTERS")
        self.assertEqual(
            long_opts,
            {k: v[0] for k, v in destructive.NO_WRITE_MODES.items()})
        self.assertEqual(
            letters,
            {k: v[1] for k, v in destructive.NO_WRITE_MODES.items()})


@unittest.skipIf(shutil.which("zsh") is None, "zsh not installed")
class ZshAdapterBehaviourParityTest(unittest.TestCase):
    """The table-sync tests prove the two sides carry the same DATA. They cannot
    see a parsing divergence, which is where the real risk is: zsh judging a
    command safe that Python judges destructive means NO snapshot is taken before
    real data loss, and the preexec path has no conservative fallback.

    Two such divergences were live before this test existed:
      - `r'm' -rf notes` runs `rm -rf notes` in zsh (adjacent quoted and unquoted
        fragments are one word), but the adapter stripped a single quote by hand
        and saw a verb literally named `r'm'` -> judged SAFE.
      - `echo 'safe; rm mentioned'` fired, because segments were split by
        replacing every `;` in the raw string, quotes included.
    """

    CASES = [
        # quoting of the verb -- all five forms run `rm`
        "rm -rf notes", "r'm' -rf notes", "\\rm -rf notes", "'rm' -rf notes",
        '"rm" -rf x',
        # separators inside quotes are not separators
        "echo 'safe; rm mentioned'", 'echo "a && rm b"', 'echo "rm -rf /"',
        "grep -r 'rm -rf' .", "echo 'a;b' && ls",
        # real separators still split
        "make && rm -rf build", "make; rm -rf build", "make || rm -rf build",
        "if true; then rm -rf x; fi", "for f in *; do rm $f; done",
        # wrappers
        "sudo rm -rf /tmp/x", "env FOO=1 rm x", "timeout 5 rm x",
        "nice -n 10 rm x", "xargs rm",
        # words containing spaces survive segmentation
        "cp 'my file.txt' 'other file.txt'", "rm 'file with space'",
        # git / in-place editors
        "git checkout .", "git status", "git stash list", "git branch -D x",
        "sed -i.bak s/a/b/ f", "sed s/a/b/ f", "perl -i -pe s/a/b/ f",
        "awk '{print}' f",
        # overwrite class + clusters + attached values
        "cp new old", "mv a b", "install -m 755 a b", "rsync -av s d",
        "tar -xf a.tar", "tar -cf a.tar d", "tar -tf a.tar",
        "tar -xvfnotes.tar", "tar -tvfxyz.tar", "tar -xvfOut.tar",
        "ln -sf a b", "ln -s a b", "unzip -o a.zip", "unzip a.zip",
        # no-write modes
        "rsync -avn s d", "rsync --dry-run s d", "rsync -n --delete s d",
        "tar -xOf a.tar f", "tar xOf a.tar f", "tar --to-stdout -xf a.tar",
        # redirects (judged separately, and they override a no-write mode)
        "echo x > out.txt", "echo x >> out.txt", "echo x > /dev/null",
        "echo x 2>&1", "rsync -avn s d > out.txt", "tar -xOf a.tar f > out.txt",
        # misc
        "cat a | tee b", "cat a | tee -a b", "tee /dev/null", "ls | grep x",
        "echo hello", "truncate -s 0 f", "dd if=a of=b", "shred f", "gzip f",
        "xz f", "patch < p.diff",
    ]

    @classmethod
    def setUpClass(cls):
        adapter = os.path.join(
            helpers.PKG_ROOT, "adapters", "git-safepoint-preexec.zsh"
        )
        driver = (
            "source {0}\n"
            "while IFS= read -r line; do\n"
            "  if _git_safepoint_is_destructive \"$line\"; then echo 1; "
            "else echo 0; fi\n"
            "done\n"
        ).format(shlex.quote(adapter))
        proc = subprocess.run(
            ["zsh", "-c", driver],
            input="\n".join(cls.CASES) + "\n",
            capture_output=True, text=True,
        )
        cls.proc = proc
        cls.zsh_results = proc.stdout.strip().split("\n") if proc.stdout else []

    def test_adapter_emits_nothing_on_stdout_but_the_verdicts(self):
        """A stray `local` on an already-declared variable makes zsh PRINT it.
        That output lands on the user's terminal on every command through
        preexec, and it silently desynchronises this very comparison."""
        self.assertEqual(
            len(self.zsh_results), len(self.CASES),
            "adapter wrote extra lines to stdout: {0!r}".format(
                self.proc.stdout[:400]),
        )

    def test_python_and_zsh_agree_on_every_case(self):
        mismatches = [
            (cmd, destructive.looks_destructive(cmd), res == "1")
            for cmd, res in zip(self.CASES, self.zsh_results)
            if destructive.looks_destructive(cmd) != (res == "1")
        ]
        self.assertEqual(
            mismatches, [],
            "\n".join("  {0!r}: python={1} zsh={2}".format(*m)
                      for m in mismatches),
        )
