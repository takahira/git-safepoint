#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared test helpers: build a throwaway git repo in a temp dir."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

# Make the package importable when tests are run from anywhere.
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

# Isolate EVERY git invocation in the test process -- the helper's own calls AND
# the engine's (which inherit os.environ via gitutil) -- from the contributor's
# global / system git config. Without this a global ``core.hooksPath`` / commit
# template fires on ``git commit``, a non-UTF-8 ``core.quotePath``, a
# ``core.autocrlf`` that rewrites blob bytes, or a non-``main``
# ``init.defaultBranch`` cause false failures in a clean checkout.
# gitutil scrubs GIT_CONFIG_GLOBAL/SYSTEM, so we steer config via the
# env vars it does NOT scrub: an empty HOME/XDG_CONFIG_HOME (no global config) and
# GIT_CONFIG_NOSYSTEM (no system config). This mirrors gitutil._child_env's
# env-scrubbing philosophy, extended to the global/system config FILES.
_ISO_HOME = tempfile.mkdtemp(prefix="git-safepoint-test-home-")
os.environ["HOME"] = _ISO_HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_ISO_HOME, ".config")
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")


def git(repo, *args, input_bytes=None, check=True):
    return subprocess.run(
        ["git", "-C", repo] + list(args),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def make_repo():
    """Create a fresh git repo in a temp dir; return its absolute path."""
    repo = tempfile.mkdtemp(prefix="git-safepoint-test-")
    # Pin the initial branch so the isolated-config env (no init.defaultBranch)
    # yields a deterministic name and no "hint:" noise.
    git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    git(repo, "config", "user.email", "test@local")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def write_file(repo, rel, content, executable=False):
    abs_path = os.path.join(repo, rel)
    parent = os.path.dirname(abs_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    if isinstance(content, str):
        content = content.encode("utf-8")
    with open(abs_path, "wb") as fh:
        fh.write(content)
    if executable:
        os.chmod(abs_path, 0o755)
    return abs_path


def read_file(repo, rel):
    with open(os.path.join(repo, rel), "rb") as fh:
        return fh.read()


def cleanup(repo):
    shutil.rmtree(repo, ignore_errors=True)


def _ls_tree_modes(repo, snap_id):
    """{path: mode} for every entry in a snapshot tree (blobs and gitlinks)."""
    from git_safepoint import engine

    ref = engine.resolve_ref(snap_id)
    out = git(repo, "ls-tree", "-r", ref).stdout.decode("utf-8", "replace")
    modes = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 1 and path:
            modes[path] = parts[0]
    return modes
