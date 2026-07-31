#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inline-secret masking for snapshot labels.

``secret.py`` keeps credential-shaped FILES out of the object store. This module
covers the other half: the snapshot LABEL. Both adapters put the triggering
command straight into it --

    adapters/git-safepoint-preexec.zsh:  --label "pre-shell: ${cmd}"
    the ``hook`` subcommand:             "pre-bash: <command>"

-- and the label becomes the commit subject via :func:`engine._make_message`, so it
is persisted in ``refs/snapshots/*`` and printed by ``list``. A command line is a
*more* common place for a credential than a filename (``export TOKEN=…``,
``-p<password>``, ``curl -H "Authorization: …"``), so leaving it unmasked defeated
the module docstring in ``secret.py``: "the safety net must still never slurp
credentials into the object store".

Masking is applied in ``_make_message`` -- the single point every path funnels
through (preexec, hook, and a hand-written ``--label``) -- so no caller can bypass
it. The rules mirror agent-trail's ``_apply_secret_subs``, which was written against
the same threat.

Scope note: this masks what goes INTO new snapshots. Labels already written to
existing snapshots are unaffected; use ``prune`` to age them out.
"""
from __future__ import annotations

import re
from typing import List, Tuple

MASK = "<redacted>"

# Auth scheme words that are never themselves the credential. The Authorization rule
# runs first and leaves ``Authorization: Bearer <redacted>``; the generic key/value
# rule (whose key alternation includes "auth") would then match that span with
# "Bearer" as the value and mask the scheme too. Skip those.
_SCHEME_WORDS = frozenset({"bearer", "basic", "token", "digest"})

# Ordered: PEM first so nothing nibbles its interior, then the Authorization header
# before the generic key/value rule, then the scoped flag rules, then bare token
# shapes. Each entry is (pattern, number of leading groups to keep verbatim).
_RULES: List[Tuple[re.Pattern, int]] = [
    # Authorization: <scheme> <credential> -- keep the scheme.
    (re.compile(r"(?i)(Authorization:\s*(?:Bearer|Basic|Token|Digest)\s+)([^\s\"';|&]+)"), 1),
    # --password VALUE / --token=VALUE
    (re.compile(
        r"(?i)(--(?:password|passwd|token|secret|api-?key|access-?key|auth|credential)(?:=|\s+))"
        r"(\"[^\"]*\"|'[^']*'|[^\s;|&]+)"), 1),
    # sshpass -p<pass>; mysql/mariadb/mongosh -p; redis-cli -a. Scoped on purpose:
    # ``docker -p`` is a port and ``psql -p`` is a port. The span from the tool name
    # to the flag is captured so the audit value (what ran, where it connected) is
    # kept -- masking must not eat the command name. The -p/-a value is either a
    # QUOTED string ('...' / "...") or a bare run that must start alnum so a bare
    # ``-p -e ...`` (prompt form) does not mask the next flag. The quoted
    # alternatives matter: ``-p'Hunter2'`` passed through UNmasked before they
    # were added, because the bare form rejects a leading quote.
    (re.compile(r"(?i)(\bsshpass\b[^\n]{0,120}?\s-p)\s*([^\s;|&]+)"), 1),
    (re.compile(r"(?i)(\b(?:mysql|mysqldump|mariadb|mongosh)\b[^\n]{0,300}?\s-p)\s*"
                r"('[^']*'|\"[^\"]*\"|[A-Za-z0-9][^\s;|&]*)"), 1),
    (re.compile(r"(?i)(\bredis-cli\b[^\n]{0,300}?\s-a)\s*"
                r"('[^']*'|\"[^\"]*\"|[A-Za-z0-9][^\s;|&]*)"), 1),
    # curl basic auth: -u user:pass / --user user:pass (colon shape required).
    (re.compile(r"(?i)((?:^|\s)(?:-u\s*|--user[=\s]\s*)[^\s:;|&]+:)([^\s;|&]+)"), 1),
]

# KEY=VALUE / KEY: VALUE with a credential-shaped key. Handled separately so the
# scheme-word skip above can apply. The runs around the keyword are BOUNDED
# ({0,40}, mirroring agent-trail's KV_SECRET_RE): an unbounded leading run makes
# the engine rescan a long alphanumeric stretch from every start position --
# quadratic on exactly the input ``hook`` produces (a whole Bash command as the
# label). Real env-var / flag names fit comfortably in 40 chars.
_KV_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]{0,40}"
    r"(?:passwd|password|secret|token|api[_-]?key|access[_-]?key|auth|credential|private[_-]?key)"
    r"[A-Za-z0-9_.-]{0,40}\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s;|&]+)")

# scheme://user:pass@host -- mask only the password, keep user and host. The scheme
# run is bounded ({0,15}, mirroring agent-trail's URL_CRED_RE) for the same
# quadratic-rescan reason as _KV_RE; no real URI scheme is longer.
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]{0,15}://[^\s:@/]*:)([^\s@/]+)(@)")

# Bare ``Bearer <token>`` outside a header.
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._~+/-]{12,}=*)")

# Provider token shapes recognisable on their own.
_TOKEN_SHAPE_RES = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),        # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),      # GitHub fine-grained
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),         # Anthropic (before sk-)
    re.compile(r"\bsk-[A-Za-z0-9-]{16,}"),              # OpenAI-style
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),      # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),           # Google API key
]

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)

# A PEM whose END marker was sheared off by the _REDACT_BOUND cap. Applied only to
# capped input (an unterminated BEGIN in a short label is not credential material
# and stays visible, as before).
_PEM_OPEN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL)

# Redaction cost grows with input length, and ``cmd_hook`` passes the WHOLE Bash
# command as the label -- tens of KB for a heredoc-heavy command -- while the stored
# label is truncated to well under this bound anyway (engine.LABEL_MAX, 60 chars,
# applied in engine._make_message AFTER redaction). So cap the input before the
# regex pass: everything past the bound can never be stored, and the pass stays
# O(bound) instead of stalling the pre-Bash hook on a huge command (measured
# quadratic before the cap: 2,000 chars ~0.12s, 16,000 chars ~7s).
#
# The bound is deliberately far larger than LABEL_MAX (mirroring agent-trail's
# _REDACT_MARGIN of 4096): redact-then-truncate only keeps a secret straddling the
# LABEL_MAX boundary from being sheared out of its own mask if the whole secret is
# visible to the rules, so the window past LABEL_MAX must comfortably exceed any
# realistic inline credential.
_REDACT_BOUND = 4096


def _kv_sub(match: "re.Match") -> str:
    value = match.group(2).strip("\"'").lower()
    if value in _SCHEME_WORDS or value == MASK.lower():
        return match.group(0)
    return match.group(1) + MASK


def redact_label(text: str) -> str:
    """Mask inline credentials in a snapshot label.

    Keeps the readable prefix (key name, flag, scheme, tool name, host) and replaces
    only the credential, so the label still says what ran.

    Input longer than ``_REDACT_BOUND`` is capped before the regex pass (the tail is
    dropped, not returned unredacted): callers store at most the first
    ``engine.LABEL_MAX`` chars, and an uncapped pass is quadratic on long labels.
    """
    if not text:
        return text
    capped = len(text) > _REDACT_BOUND
    if capped:
        # The tail can never reach the stored label (LABEL_MAX truncation follows),
        # so dropping it loses nothing and keeps the regex pass bounded.
        text = text[:_REDACT_BOUND]
    out = _PEM_RE.sub(MASK, text)
    if capped:
        # The cap may have cut a PEM in half, leaving a BEGIN with no END for
        # _PEM_RE to anchor on; mask from the BEGIN marker to the cut instead of
        # letting key material survive as a "non-matching" fragment.
        out = _PEM_OPEN_RE.sub(MASK, out)
    for pattern, _keep in _RULES:
        out = pattern.sub(lambda m: m.group(1) + MASK, out)
    out = _KV_RE.sub(_kv_sub, out)
    out = _URL_CRED_RE.sub(lambda m: m.group(1) + MASK + m.group(3), out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + MASK, out)
    for pattern in _TOKEN_SHAPE_RES:
        out = pattern.sub(MASK, out)
    return out
