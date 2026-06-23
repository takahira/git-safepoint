#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect whether a Bash command line is destructive (head-verb allowlist).

By design we do NOT use substring matching (the spike's ``tok in c``
mis-fires on ``grep -r rm src/``, ``find . -name rmdir``, ``echo rm >> notes``).
Instead we split the command into pipeline / list segments and inspect the
*first token* of each segment against an allowlist, after stripping wrapper
prefixes (``env``, ``sudo``, ``time``, ``nohup``...). Truncating output
redirection (``>``, ``>|``, ``&>``, and ``>& file``, but not ``>>`` or fd-dups
like ``2>&1``) is detected as a separate destructive form.
"""
from __future__ import annotations

import os
import shlex
from typing import List

# First-token verbs that can destroy uncommitted work.
DESTRUCTIVE_VERBS = {
    "rm",
    "rmdir",
    "mv",
    "truncate",
    "dd",
    "shred",
    # In-place compressors REPLACE their input (``gzip f`` -> ``f.gz``, original
    # removed) and ``patch`` rewrites target files from a diff. Fired on the verb
    # without flag inspection (over-fire on ``gzip -l`` is a harmless extra,
    # deduped snapshot; under-firing risks losing the original).
    "gzip",
    "bzip2",
    "xz",
    "patch",
}

# Verbs that overwrite a file ARGUMENT (no shell redirect) unless an append flag
# is present. ``tee out`` truncates ``out``; ``tee -a out`` appends.
DESTRUCTIVE_UNLESS_APPEND = {
    "tee": {"-a", "--append"},
}

# git subcommands that can clobber the work tree.
DESTRUCTIVE_GIT_SUBCMDS = {
    "checkout",
    "switch",  # any git switch can discard uncommitted work (over-fires like the other subcmds)
    "restore",
    "reset",
    "clean",
    "rm",
    "stash",  # `git stash` can hide uncommitted work
}

# `git branch` is only destructive with a delete flag (`-D` / `-d` / `--delete`).
_GIT_BRANCH_DELETE_FLAGS = {"-D", "-d", "--delete"}

# Commands that are only destructive when a specific in-place flag is present.
# e.g. ``sed -i``, ``awk -i inplace``, ``perl -i``.
DESTRUCTIVE_FLAG_CMDS: "dict[str, set[str]]" = {
    "sed":  {"-i", "--in-place"},
    "awk":  {"-i"},
    "perl": {"-i"},
}

# Wrapper commands to peel off before reading the real verb. These run the
# *next* word as a command, so the real destructive verb surfaces once the
# wrapper is stripped (`sudo rm`, `env FOO=bar rm`). `xargs` is included so a
# pipeline tail like `... | xargs rm` is judged on `rm`.
#
# Known gaps (NOT covered, tested in test_destructive):
#
# 1. Destructive verb hidden in an argument (not the segment head):
#      find . -exec rm {} ;        judged as `find` → miss
#      xargs -I{} rm {}            xargs IS a wrapper, but only when it is the
#                                  segment head; mid-pipeline ``… | xargs rm``
#                                  is caught because xargs is stripped as a
#                                  wrapper prefix.
#
# 2. Command substitution / subshells:
#      echo $(rm -rf x)            head verb is `echo` → miss
#      (rm -rf x)                  head verb is literal `(` → miss
#      bash -c "rm -rf x"          head verb is `bash`; the -c body is not
#                                  parsed → miss. Same for `sh -c`, `zsh -c`.
#
# 3. Scripting languages with destructive code in the script body:
#      python3 -c "open('f','w').write('')"   head verb is `python3` → miss
#      python3 script.py                       script content not inspected
#      ruby -e "File.write('f','')"           same
#      node -e "fs.writeFileSync('f','')"     same
#    Unlike `perl -i` (a dedicated in-place flag) or `sed -i`, these languages
#    embed file-write intent in arbitrary code; reliable detection would require
#    language-level parsing and is out of scope for a head-verb allowlist.
#
# 4. Append-only redirection (>>) is intentionally NOT flagged; only truncating
#    redirects (>, >|, &>, and `>& file` with a filename target) fire. An append
#    to a large file can still cause data loss but the false-positive cost of
#    flagging >> everywhere is too high.
#
# The head-verb allowlist deliberately trades these misses for a near-zero
# false-positive rate.
WRAPPERS = {
    "env",
    "sudo",
    "time",
    "nohup",
    "command",
    "exec",
    "builtin",
    "nice",
    "xargs",
    "timeout",
    "stdbuf",
    "setsid",
    "ionice",
    "doas",
}

# Wrapper options that consume the FOLLOWING token as a value, so the value must
# be skipped rather than mistaken for the real command head. Without this,
# ``sudo -u root rm`` reads ``root`` as the verb and misses the ``rm``.
_WRAPPER_VALUE_OPTS: "dict[str, set[str]]" = {
    "sudo":  {"-u", "-g", "-U", "-p", "-C", "-r", "-t", "-h",
              "--user", "--group", "--prompt", "--role", "--type"},
    "doas":  {"-u", "-C"},
    "nice":  {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    # NOTE: of the ``-i``/``-I`` pair, only ``-I`` takes a SEPARATE next-token
    # value; ``-i`` is the deprecated glued alias whose replace-string is
    # optional and GLUED (``-iREPL`` /
    # bare ``-i`` == {}), so it must NOT consume the following token -- doing so
    # ate the command in ``xargs -i rm {}`` and skipped the snapshot.
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a",
              "--replace", "--max-lines", "--max-args", "--max-procs",
              "--delimiter", "--arg-file", "--eof"},
    "env":   {"-u", "-S", "--unset", "--split-string", "-C", "--chdir"},
}

# git global options that take a value (so we must skip the value, not read it
# as the subcommand): `git -C <dir> restore` etc.
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

# Control operators that separate one command segment from the next. shlex with
# ``punctuation_chars`` groups runs of ``();<>|&`` into operator tokens, so these
# match even when glued to a word (``echo hi;rm`` -> ``echo``, ``hi``, ``;`` ...).
_SEPARATOR_TOKENS = {";", "&&", "||", "|", "|&", "&"}

# Operator tokens that TRUNCATE a file target (vs append ``>>`` / fd-dup ``>&`` /
# input ``<``). ``&>`` redirects both stdout+stderr to a file (truncating).
_TRUNCATING_OPS = {">", ">|", "&>"}

# Redirect targets that are never destructive (truncating them is a no-op).
_NULL_TARGETS = {"/dev/null"}


def _split_lines(command: str) -> List[str]:
    """Split on UNQUOTED newlines into statements.

    A newline is a shell command separator, but shlex treats it as ordinary
    whitespace -- so a multi-line script collapses into one segment and only the
    first verb is judged. We split first, honouring single/double quotes and
    backslash escapes, then tokenise each line.
    """
    lines: List[str] = []
    buf: List[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == "\n" and not in_single and not in_double:
            lines.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    lines.append("".join(buf))
    return [ln for ln in lines if ln.strip()]


def _tokenize(line: str) -> List[str]:
    """Tokenise one newline-free line, grouping control/redirect operators.

    ``punctuation_chars=True`` makes shlex emit operator tokens (``&&``, ``|``,
    ``;``, ``>``, ``>>``, ``>&`` ...) separately from words, even when glued on,
    which an exact-match-only separator check would have missed.
    Raises ``ValueError`` on unbalanced quotes (caller fires, erring toward a
    snapshot).
    """
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _is_redirect_op(tok: str) -> bool:
    """True for a redirect operator token (``>``, ``>>``, ``>|``, ``>&``, ``<`` ...).

    Separator tokens are matched before this is called, so a token made only of
    ``<>&|`` that contains ``<`` or ``>`` is a redirect (not a pipe/and/or).
    """
    return bool(tok) and all(c in "<>&|" for c in tok) and (">" in tok or "<" in tok)


def _has_truncating_redirect(tokens: List[str]) -> bool:
    """True when a token sequence truncates a real file (not /dev/null, not an
    fd-dup like ``2>&1``, not an append ``>>``)."""
    for i, tok in enumerate(tokens):
        target = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _TRUNCATING_OPS:
            if target and target not in _NULL_TARGETS:
                return True
        elif tok == ">&":
            # ``>& file`` (bash/zsh) truncates BOTH stdout+stderr into a real
            # file, but ``>&2`` / ``2>&1`` fd-dup with a NUMERIC target does not.
            # Fire only when the target is a filename.
            if target and target not in _NULL_TARGETS and not target.isdigit():
                return True
    return False


def _segments_from_tokens(tokens: List[str]) -> List[List[str]]:
    """Split a token list into command segments on control operators.

    Redirect operator tokens (and their nature) are handled separately by
    :func:`_has_truncating_redirect`; here we drop them so a segment's head verb
    is the real command, not a redirect glyph.
    """
    segments: List[List[str]] = []
    current: List[str] = []
    for tok in tokens:
        if tok in _SEPARATOR_TOKENS:
            if current:
                segments.append(current)
                current = []
        elif _is_redirect_op(tok):
            continue
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _looks_like_duration(tok: str) -> bool:
    """True for a ``timeout`` DURATION positional like ``5``, ``1.5``, ``30s``."""
    if not tok:
        return False
    body = tok[:-1] if tok[-1] in "smhd" else tok
    try:
        float(body)
        return True
    except ValueError:
        return False


def _strip_wrappers(tokens: List[str]) -> List[str]:
    """Drop leading wrapper words, their flags+values, and ``VAR=value`` prefixes.

    e.g. ``xargs -0 rm`` -> ``rm``, ``sudo -u root rm`` -> ``rm``,
    ``nice -n 10 rm`` -> ``rm``, ``timeout 5 rm`` -> ``rm``, ``FOO=bar rm`` ->
    ``rm``. Value-taking wrapper options (``-u``/``-n``/...) consume their value
    so it is not mistaken for the command head, and ``timeout``'s positional
    DURATION is skipped too.
    """
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        verb = os.path.basename(tok) if "/" in tok else tok
        if verb in WRAPPERS:
            value_opts = _WRAPPER_VALUE_OPTS.get(verb, set())
            idx += 1
            # Skip this wrapper's flags. A value-taking option that does not glue
            # its value with ``=`` also consumes the following token.
            while idx < n and tokens[idx].startswith("-"):
                opt = tokens[idx]
                takes_value = "=" not in opt and opt in value_opts
                idx += 1
                if takes_value and idx < n:
                    idx += 1
            # ``timeout [opts] DURATION cmd``: the duration is a bare positional.
            if verb == "timeout" and idx < n and _looks_like_duration(tokens[idx]):
                idx += 1
            continue
        # Leading environment assignment like FOO=bar cmd ...
        if "=" in tok and not tok.startswith("=") and "/" not in tok.split("=")[0]:
            head = tok.split("=", 1)[0]
            if head.replace("_", "").isalnum():
                idx += 1
                continue
        break
    return tokens[idx:]


# Shell reserved words / compound-command prefixes that can sit at a segment
# head in front of the real verb. The segment splitter only breaks on the control
# operators in ``_SEPARATOR_TOKENS`` (``; && || | |& &``), so the word after a
# ``;`` in ``if ...; then rm -rf x; fi`` / ``for ...; do rm x; done`` / a brace
# group ``{ rm x; }`` becomes the head token and hides a plain destructive verb.
# Stripping these (over-fire is harmless) lets the real verb be judged. Subshell
# ``(`` is intentionally NOT stripped: its command-substitution sibling
# ``echo $(rm x)`` stays uncaught and it is a documented limitation.
_COMPOUND_PREFIXES = {"then", "do", "else", "elif", "{", "!"}


def _strip_compound_prefixes(tokens: List[str]) -> List[str]:
    """Drop leading shell reserved-word / compound-command prefixes from a segment."""
    idx = 0
    while idx < len(tokens) and tokens[idx] in _COMPOUND_PREFIXES:
        idx += 1
    return tokens[idx:]


def _git_subcommand(tokens: List[str]) -> str:
    """Return git's effective subcommand, skipping global options + values."""
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in _GIT_VALUE_OPTS:
            i += 2  # skip the option AND its value (e.g. -C <dir>)
            continue
        if t.startswith("-"):
            i += 1  # other global flag with no separate value
            continue
        return t
    return ""


def segment_is_destructive(tokens: List[str]) -> bool:
    if not tokens:
        return False
    # Peel `then`/`do`/`else`/`elif`/`{`/`!` first so the real verb in an
    # if/for/while body or brace group is judged, then peel command wrappers
    # (`sudo`/`env`/`xargs`/...).
    tokens = _strip_compound_prefixes(tokens)
    if not tokens:
        return False
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return False
    verb = os.path.basename(tokens[0]) if "/" in tokens[0] else tokens[0]
    if verb in DESTRUCTIVE_VERBS:
        return True
    # File-overwriting verbs that truncate an arg unless an append flag is given.
    if verb in DESTRUCTIVE_UNLESS_APPEND:
        append_flags = DESTRUCTIVE_UNLESS_APPEND[verb]
        if not any(t in append_flags for t in tokens[1:]):
            # ``tee`` truncates its FILE ARGUMENTS; a bare ``tee`` (or only
            # ``tee /dev/null``) is a harmless stdin->stdout pass-through, so do
            # not fire on it -- otherwise every ``... | tee`` spams a snapshot
            # Require a real, non-/dev/null file target.
            targets = [
                t for t in tokens[1:]
                if not t.startswith("-") and t not in _NULL_TARGETS
            ]
            if targets:
                return True
    if verb == "git":
        # Intentional over-fire (accepted): a bare destructive git
        # subcommand fires on the subcommand name without flag inspection, so
        # safe forms (``git stash list``, mixed ``git reset``, ``git restore
        # --staged``, ``git checkout -b``) also trigger a snapshot. Over-firing
        # on the snapshot path is harmless (an extra, deduped capture); under-
        # firing would risk data loss, so we err toward firing.
        sub = _git_subcommand(tokens)
        if sub in DESTRUCTIVE_GIT_SUBCMDS:
            return True
        # `git branch` only clobbers refs with a delete flag.
        if sub == "branch" and any(t in _GIT_BRANCH_DELETE_FLAGS for t in tokens):
            return True
    # Flag-gated in-place editors: sed -i, awk -i, perl -i, and glued suffix
    # forms like ``sed -i.bak`` (a short ``-i`` flag followed by a backup suffix).
    # The glued ``-i*`` match also fires on awk's unrelated ``-include`` (an extra,
    # deduped snapshot): an accepted over-fire -- the safe direction,
    # since narrowing it would risk MISSING a real ``sed -ibak`` in-place edit.
    if verb in DESTRUCTIVE_FLAG_CMDS:
        flags = DESTRUCTIVE_FLAG_CMDS[verb]
        for t in tokens[1:]:
            if t in flags:
                return True
            for f in flags:
                # ``-i=...`` / ``--in-place=...`` or a short ``-i`` with a glued
                # suffix (``-i.bak``). Restrict the glued case to 2-char flags so
                # ``--in-place`` does not match unrelated long options.
                if t.startswith(f + "=") or (len(f) == 2 and t.startswith(f)):
                    return True
    return False


def looks_destructive(command: str) -> bool:
    """True when any segment's head verb is destructive or a file is truncated.

    Multi-line scripts are split on unquoted newlines first; each line is
    tokenised with operator grouping so destructive verbs glued to a separator
    (``a;rm``, ``a&&rm``) or living on a later line are still seen. On a
    tokenisation error (unbalanced quotes) we err toward firing -- the safety net
    should snapshot precisely when a command is too ambiguous to trust.
    """
    if not command or not command.strip():
        return False
    try:
        for line in _split_lines(command):
            tokens = _tokenize(line)
            if _has_truncating_redirect(tokens):
                return True
            for seg in _segments_from_tokens(tokens):
                if segment_is_destructive(seg):
                    return True
    except ValueError:
        return True
    return False
