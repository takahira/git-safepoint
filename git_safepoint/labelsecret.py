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
    # kept -- masking must not eat the command name.
    (re.compile(r"(?i)(\bsshpass\b[^\n]{0,120}?\s-p)\s*([^\s;|&]+)"), 1),
    (re.compile(r"(?i)(\b(?:mysql|mysqldump|mariadb|mongosh)\b[^\n]{0,300}?\s-p)\s*([A-Za-z0-9][^\s;|&]*)"), 1),
    (re.compile(r"(?i)(\bredis-cli\b[^\n]{0,300}?\s-a)\s*([A-Za-z0-9][^\s;|&]*)"), 1),
    # curl basic auth: -u user:pass / --user user:pass (colon shape required).
    (re.compile(r"(?i)((?:^|\s)(?:-u\s*|--user[=\s]\s*)[^\s:;|&]+:)([^\s;|&]+)"), 1),
]

# KEY=VALUE / KEY: VALUE with a credential-shaped key. Handled separately so the
# scheme-word skip above can apply.
_KV_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]*"
    r"(?:passwd|password|secret|token|api[_-]?key|access[_-]?key|auth|credential|private[_-]?key)"
    r"[A-Za-z0-9_.-]*\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s;|&]+)")

# scheme://user:pass@host -- mask only the password, keep user and host.
_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:@/]*:)([^\s@/]+)(@)")

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


def _kv_sub(match: "re.Match") -> str:
    value = match.group(2).strip("\"'").lower()
    if value in _SCHEME_WORDS or value == MASK.lower():
        return match.group(0)
    return match.group(1) + MASK


def redact_label(text: str) -> str:
    """Mask inline credentials in a snapshot label.

    Keeps the readable prefix (key name, flag, scheme, tool name, host) and replaces
    only the credential, so the label still says what ran.
    """
    if not text:
        return text
    out = _PEM_RE.sub(MASK, text)
    for pattern, _keep in _RULES:
        out = pattern.sub(lambda m: m.group(1) + MASK, out)
    out = _KV_RE.sub(_kv_sub, out)
    out = _URL_CRED_RE.sub(lambda m: m.group(1) + MASK + m.group(3), out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + MASK, out)
    for pattern in _TOKEN_SHAPE_RES:
        out = pattern.sub(MASK, out)
    return out
