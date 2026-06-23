#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Always-on secret exclusion for opt-in ignored capture.

When the user opts in to capturing ``.gitignore``'d paths (``--include-ignored``)
the safety net must still never slurp credentials into the object store. This
module is the second stage of the two-stage design:

1. ``--include-ignored <pattern>...`` widens enumeration to ignored paths that
   match the user's explicit allow patterns (e.g. ``output/``).
2. Any UNTRACKED path whose basename or relative path matches a secret pattern
   here is dropped, opt-in or not -- there is no flag to disable this floor.

Scope of the floor: it applies only to paths NOT tracked in the
index. A file already tracked by git has its blob in the object store regardless,
so snapshotting it leaks nothing new -- and excluding it would leave a tracked
``id_rsa`` / ``server.pem`` / ``credentials.json`` UNPROTECTED with zero secrecy
benefit. The tracked-vs-untracked split is applied by the caller
(:func:`git_safepoint.gitutil.list_worktree_files`); this module only decides
whether a *name* looks like a secret.

Consequence to keep in mind: dropping an untracked file IS a protection gap (the
file is then never snapshotted), not a free "never data loss" -- so the pattern
list errs toward narrow, name-targeted globs, and the tracked-file exemption
above keeps already-committed work always covered. The matcher is path-based
(filename / glob), not content-based: content sniffing is deliberately refused
(false-positive / false-negative liability). This list is the floor, not the
ceiling; a user who needs to keep an untracked secret-looking file out of the
floor can ``git add`` it (it is then tracked and exempt).
"""
from __future__ import annotations

import fnmatch
import os
from typing import List, Optional

# Default always-excluded secret patterns. Matched against BOTH the basename
# and the full relative path (so ``config/id_rsa`` and bare ``id_rsa`` both hit)
# with shell-style globs, case-insensitively.
DEFAULT_SECRET_PATTERNS: List[str] = [
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.p8",          # Apple AuthKey / PKCS#8 private key
    "*.keystore",
    "*.jks",         # Java/Android keystore (sibling of *.keystore)
    "*.secret",
    "*.secrets",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.ppk",
    "credentials",
    ".netrc",
    ".pgpass",
    "*.crt",
    "secring.*",
    # --- additions: common high-value credential filenames. The
    # floor is name-based and conservative; an over-broad glob only drops an
    # UNTRACKED file (tracked files are exempt), so we err toward
    # inclusion. ---
    ".git-credentials",          # plaintext https://user:pass@host
    ".npmrc",                    # npm auth tokens
    ".pypirc",                   # PyPI upload tokens
    ".dockercfg",                # legacy docker registry auth
    ".docker/config.json",       # docker registry auth (full path)
    "kubeconfig",
    ".kube/config",
    "*.ovpn",                    # OpenVPN profile (often embeds keys)
    "*.gpg",                     # PGP key material / encrypted secrets
    "*.asc",                     # ASCII-armored PGP keys
    "*.tfvars",                  # Terraform variables (often secrets)
    "*.tfstate",                 # Terraform state (contains secrets)
    "*.tfstate.*",               # Terraform state backups
    "service-account*.json",     # GCP service-account keys
    "*-service-account*.json",
    # --- additions: names that slipped past the floor above. Each is
    # basename-targeted (``*credentials`` matches a name ENDING in credentials,
    # ``credentials.*`` a name STARTING with it) so it never matches across "/"
    # and silently drops a whole source directory. ---
    "*credentials",              # aws_credentials, git-credentials, ...
    # ``credentials.*`` (the old form) also swallowed SOURCE files literally
    # named ``credentials.py`` / ``.go`` / ``.ts`` / ``.rb`` -- an untracked new
    # module would silently never be snapshotted (over-match). Enumerate
    # the data/config extensions actually used for credential PAYLOADS instead, so
    # a source file named ``credentials.<lang>`` is still protected.
    "credentials.json",
    "credentials.y*ml",          # credentials.yml / credentials.yaml
    "credentials.xml",
    "credentials.ini",
    "credentials.toml",
    "credentials.cfg",
    "client_secret*.json",       # OAuth client secret files (.json form)
    "secrets.y*ml",              # secrets.yaml / secrets.yml
    "*.tfvars.json",             # Terraform vars (JSON form; *.tfvars misses it)
    "database.yml",              # Rails DB config (DB credentials)
    "*.cer",                     # certificate (sibling of *.crt)
    "*.der",                     # DER-encoded key/cert
    "*.p7b",                     # PKCS#7 cert bundle
    "*.csr",                     # certificate signing request
    ".htpasswd",                 # Apache basic-auth hashes
    "*.kdbx",                    # KeePass database
    "authorized_keys",           # SSH authorized keys
    "wp-config.php",             # WordPress DB credentials
    # --- additions: common credential names the floor
    # missed. Name-based and conservative; an over-broad glob only drops an
    # UNTRACKED file (tracked files are exempt), so we err toward
    # inclusion. ---
    "*firebase-adminsdk*.json",  # Firebase Admin SDK service-account key
    "secrets.json",              # generic JSON secrets (YAML form handled by secrets.y*ml above)
    "secrets.toml",
    "*.flaskenv",                # Flask env (.flaskenv / app.flaskenv; *.env misses it)
    ".my.cnf",                   # MySQL client credentials
    ".mylogin.cnf",              # MySQL login-path (obfuscated, still secret)
    "_netrc",                    # Windows variant of .netrc
    # --- additions: floor gaps for high-value credential material.
    # Name-based and conservative; with the tracked-file exemption in
    # gitutil.list_worktree_files an over-broad glob only affects an UNTRACKED
    # file, so we still err toward inclusion. ---
    "id_ecdsa_sk",               # FIDO2/U2F-backed SSH key (ecdsa-sk)
    "id_ed25519_sk",             # FIDO2/U2F-backed SSH key (ed25519-sk)
    ".envrc",                    # direnv: routinely exports secrets into the env
    "*.keytab",                  # Kerberos keytab (long-lived service creds)
    "pgpass.conf",               # Windows PostgreSQL pgpass (sibling of .pgpass)
    # --- additions: unambiguous plaintext-credential dotfiles the
    # floor missed. Single-component dot names, so the existing basename /
    # segment / backup-decoration matching all apply. ---
    ".s3cfg",                    # s3cmd config (AWS access/secret keys)
    ".boto",                     # boto/gsutil config (cloud credentials)
    ".vault-token",              # HashiCorp Vault token
]


def _norm(rel: str) -> str:
    """Normalise to forward-slash separators for stable glob matching."""
    return rel.replace(os.sep, "/")


def _is_glob(pat: str) -> bool:
    return "*" in pat or "?" in pat or "[" in pat


# Trailing extensions editors / merges append to a backup or swap copy. A copy
# of a recognised secret keeps the IDENTICAL plaintext, so its derived name must
# hit the same floor as the original. The ``.env.*`` glob already
# does this for the .env family; this generalises the intent to every family.
_BACKUP_EXTS = (".bak", ".swp", ".swo", ".save", ".orig", ".old", ".tmp")


def _strip_backup_decoration(name: str) -> str:
    """A de-decorated basename: ``.env~``->``.env``, ``id_rsa.bak``->``id_rsa``,
    ``server.pem.swp``->``server.pem``, emacs ``#.env#``->``.env``.

    Strips, at most once each, an emacs ``#...#`` wrapper, any trailing ``~``
    runs, and one trailing backup extension, so an editor/merge derivative of a
    recognised secret reduces to the name the floor already matches. Over-broad
    only drops an UNTRACKED file (tracked files are exempt),
    consistent with the floor's existing "err toward inclusion" stance.
    """
    n = name
    if len(n) >= 3 and n.startswith("#") and n.endswith("#"):
        n = n[1:-1]
    while n.endswith("~"):
        n = n[:-1]
    for ext in _BACKUP_EXTS:
        if n.endswith(ext):
            n = n[: -len(ext)]
            break
    return n


def is_secret(rel: str, patterns: Optional[List[str]] = None) -> bool:
    """True when ``rel`` matches any secret pattern (basename or full path).

    Matching is case-insensitive (``.ENV`` is still a secret) and uses
    shell-style globs. ``rel`` is a repo-relative path.

    Hardening:
    - The basename is whitespace-stripped before matching so a degenerate name
      like ``.env `` (trailing space) or ``.env\\n.bak`` cannot slip past the
      floor.
    - For non-glob, single-component floor names (``.env``, ``credentials``,
      ``id_rsa``, ...) any path SEGMENT equal to the name is treated as secret,
      so files under a directory literally named ``.env`` (``.env/config``) are
      also excluded.
    """
    pats = DEFAULT_SECRET_PATTERNS if patterns is None else patterns
    norm = _norm(rel).lower()
    segments = [s for s in norm.split("/") if s]
    base = segments[-1] if segments else ""
    # Candidate basenames defeat whitespace/control-char decoration of a secret
    # name: the raw name, the whitespace-stripped name, and the first
    # line (so ``.env `` and ``.env\n.bak`` are still caught).
    candidates = {base, base.strip(), base.split("\n", 1)[0].strip()}
    # Also test a de-decorated basename so an editor/merge backup or swap copy of
    # a recognised secret (``.env~``, ``id_rsa.bak``, ``server.pem.swp``,
    # ``#.env#``) hits the same patterns as the original.
    candidates |= {_strip_backup_decoration(c) for c in list(candidates)}
    candidates.discard("")
    for raw in pats:
        pat = raw.lower()
        if fnmatch.fnmatch(norm, pat):
            return True
        if any(fnmatch.fnmatch(cand, pat) for cand in candidates):
            return True
        # A slash-bearing floor pattern (``.kube/config``, ``.docker/config.json``)
        # only matched at the repo ROOT via fnmatch(norm, pat) above; a vendored /
        # nested copy (``vendor/.kube/config``) slipped through. Also match the
        # pattern against the trailing path segments at ANY depth, on segment
        # boundaries, so a nested copy is excluded too.
        if "/" in pat:
            depth = pat.count("/") + 1
            seg_tail = segments[-depth:]
            tail = "/".join(seg_tail)
            if fnmatch.fnmatch(tail, pat):
                return True
            # A backup/swap copy of a slash-bearing secret (``.kube/config.bak``,
            # ``.docker/config.json.swp``) keeps the same plaintext, so de-decorate
            # ONLY the last segment (preserving the directory prefix) and re-test.
            # Bare ``config.json.bak`` without the ``.kube/`` prefix still does NOT
            # match, keeping the slash-context scoping.
            if seg_tail:
                deco = "/".join(
                    seg_tail[:-1] + [_strip_backup_decoration(seg_tail[-1])]
                )
                if deco != tail and fnmatch.fnmatch(deco, pat):
                    return True
        # A directory literally named like a DOTFILE floor name (``.env``,
        # ``.netrc``, ``.git-credentials`` ...) excludes its contents too. This
        # is gated to names starting with '.' on purpose: matching every path
        # SEGMENT against ambiguous English words like ``credentials`` /
        # ``kubeconfig`` would silently drop entire source directories named that
        # way from every snapshot. The credential FILE itself is
        # still caught by the basename match above.
        if pat.startswith(".") and "/" not in pat and not _is_glob(pat):
            if any(seg == pat for seg in segments):
                return True
    return False


def drop_secrets(
    rels: List[str], patterns: Optional[List[str]] = None
) -> "tuple[List[str], List[str]]":
    """Split paths into (kept, dropped_secrets), preserving order.

    ``dropped`` is returned so callers can surface "skipped N secret file(s)"
    transparency without ever capturing them.
    """
    kept: List[str] = []
    dropped: List[str] = []
    for rel in rels:
        if is_secret(rel, patterns):
            dropped.append(rel)
        else:
            kept.append(rel)
    return kept, dropped
