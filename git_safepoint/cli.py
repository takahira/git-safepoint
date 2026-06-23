#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line interface for git-safepoint.

Common form::

    python3 -m git_safepoint --repo <path> <subcommand> ...

Exit codes: 0 = ok; 1 = nothing-to-do / benign skip; 2 = non-repo / usage /
fatal; 3 = id or path not in snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import List, Optional

from . import __version__, engine, gitutil
from .destructive import looks_destructive


def _include_ignored_from_env() -> Optional[List[str]]:
    """Read opt-in ignored patterns for the hook/preexec path from the env.

    ``GIT_SAFEPOINT_INCLUDE_IGNORED`` is a ``:`` (or ``,``) separated list, e.g.
    ``output/:dist/``. Hooks have no CLI flags, so this is how a user opts in
    to capturing ignored artifacts through Claude Code / zsh preexec. Secret
    patterns are still always excluded downstream. The legacy ``SNAPNET_*`` name
    is still honoured for backward compatibility.
    """
    raw = (
        os.environ.get("GIT_SAFEPOINT_INCLUDE_IGNORED")
        or os.environ.get("SNAPNET_INCLUDE_IGNORED")  # legacy name
        or ""
    ).strip()
    if not raw:
        return None
    parts = [p.strip() for chunk in raw.split(":") for p in chunk.split(",")]
    pats = [p for p in parts if p]
    return pats or None


def _print_snapshot_result(res: engine.SnapshotResult, quiet: bool) -> None:
    if res.status == 0:
        if quiet:
            return
        print("snapshot {0}".format(res.id))
        print("  ref:     {0}".format(res.ref))
        print("  commit:  {0}".format(res.commit))
        print("  files:   {0}".format(res.files))
        print("  reused:  {0}".format(res.reused))
        if res.skipped:
            print("  skipped: {0}".format(res.skipped))
        print("  elapsed: {0:.1f} ms".format(res.elapsed_ms))
    else:
        if res.reason:
            print(res.reason, file=sys.stderr)


def cmd_snapshot(repo: str, args: argparse.Namespace) -> int:
    # The zsh preexec adapter calls this ``snapshot`` subcommand (not ``hook``)
    # with no CLI flags, so without the env fallback GIT_SAFEPOINT_INCLUDE_IGNORED
    # was silently ignored on the preexec path even though the README advertises
    # it for "the hook/preexec path". Honour the env here too; the explicit
    # ``--include-ignored`` flag still wins.
    include_ignored = args.include_ignored or _include_ignored_from_env()
    res = engine.capture(
        repo,
        label=args.label,
        via=args.via,
        force=args.force,
        debounce=args.debounce,
        force_rehash=args.force_rehash,
        no_wait=args.no_wait,
        include_ignored=include_ignored,
    )
    _print_snapshot_result(res, args.quiet)
    return res.status


def cmd_list(repo: str, args: argparse.Namespace) -> int:
    rows = engine.list_refs(repo)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    # A concurrent prune (or any ref expiry) can delete a ref in the window
    # between list_refs and reading its metadata; snapshot_meta's rev-parse then
    # exits non-zero and would crash this read-only listing (exit 2). Skip a
    # vanished ref and keep listing the rest.
    def _meta(snap_id, ref):
        try:
            return engine.snapshot_meta(repo, snap_id, ref)
        except subprocess.CalledProcessError:
            return None

    if args.json:
        for snap_id, ref in rows:
            meta = _meta(snap_id, ref)
            if meta is None:
                continue
            sys.stdout.write(json.dumps(meta, ensure_ascii=False) + "\n")
        return 0
    if not rows:
        print("no snapshots")
        return 0
    print("snapshots (newest first):")
    for snap_id, ref in rows:
        meta = _meta(snap_id, ref)
        if meta is None:
            continue
        label = " {0}".format(meta["label"]) if meta["label"] else ""
        staged = " +staged" if meta.get("has_staged") else ""
        print(
            "  {0}  {1}  files={2}  bytes={3}  via={4}{5}{6}".format(
                snap_id,
                meta["commit"][:12],
                meta["files"],
                meta["bytes"],
                meta["via"] or "?",
                staged,
                label,
            )
        )
    return 0


def cmd_diff(repo: str, args: argparse.Namespace) -> int:
    staged = getattr(args, "staged", False)
    if args.path:
        status, text = engine.diff_file(
            repo, args.id1, args.path, args.id2, staged=staged
        )
    else:
        status, text = engine.diff_name_status(
            repo, args.id1, args.id2, staged=staged
        )
    if status == 0:
        if text:
            print(text)
        return 0
    print(text, file=sys.stderr)
    return status


def _interactive_restore(repo: str, args: argparse.Namespace) -> int:
    """TTY flow: list snapshots -> pick -> name-status diff -> confirm restore.

    Implements `restore --interactive`. A non-TTY cannot drive the selection
    flow, so it is rejected with exit code 2 (a destructive operation is never
    run blindly).
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "restore --interactive requires a TTY (use restore <id> <path> / "
            "--dir / --all in non-interactive mode)",
            file=sys.stderr,
        )
        return 2

    rows = engine.list_refs(repo)
    if not rows:
        print("no snapshots to restore from", file=sys.stderr)
        return 1

    snap_id = args.id
    if snap_id is None:
        # A concurrent prune can delete a ref between list_refs and reading its
        # metadata; skip a vanished ref (like cmd_list) and SELECT
        # against the surviving rows so the printed index maps to a live ref.
        # Without this, snapshot_meta would abort the whole flow, or
        # the index could point at a deleted ref.
        survivors = []
        print("snapshots (newest first):")
        for sid, ref in rows:
            try:
                meta = engine.snapshot_meta(repo, sid, ref)
            except subprocess.CalledProcessError:
                continue
            label = " {0}".format(meta["label"]) if meta["label"] else ""
            print(
                "  [{0}] {1}  files={2}  via={3}{4}".format(
                    len(survivors), sid, meta["files"], meta["via"] or "?", label
                )
            )
            survivors.append((sid, ref))
        if not survivors:
            print("no snapshots to restore from", file=sys.stderr)
            return 1
        try:
            sel = input("select snapshot index (or 'q' to abort): ").strip()
        except EOFError:
            sel = "q"
        if sel.lower() in ("q", "quit", ""):
            print("aborted", file=sys.stderr)
            return 1
        try:
            idx = int(sel)
            if not (0 <= idx < len(survivors)):
                raise ValueError("index out of range")
            snap_id = survivors[idx][0]
        except (ValueError, IndexError):
            print("invalid selection", file=sys.stderr)
            return 2

    staged = getattr(args, "staged", False)
    status, text = engine.diff_name_status(repo, snap_id, None, staged=staged)
    if status != 0:
        print(text, file=sys.stderr)
        return status
    scope = "staged variant" if staged else "snapshot"
    print("diff ({0} {1} -> current work tree):".format(scope, snap_id))
    print(text if text else "  (no differences)")

    try:
        ans = input(
            "restore ALL files from {0}? this overwrites the work tree "
            "[y/N] ".format(snap_id)
        )
    except EOFError:
        ans = ""
    if ans.strip().lower() not in ("y", "yes"):
        print("aborted", file=sys.stderr)
        return 1
    rstatus, msg = engine.restore_all(
        repo, snap_id, args.no_backup, staged=staged
    )
    print(msg, file=sys.stderr if rstatus else sys.stdout)
    return rstatus


def cmd_restore(repo: str, args: argparse.Namespace) -> int:
    no_backup = args.no_backup
    if args.interactive:
        if args.path or args.dir or args.all:
            # The interactive flow drives its own snapshot/scope selection, so an
            # explicit <path>/--dir/--all would otherwise be silently ignored.
            # Say so rather than appear to honour it.
            print(
                "restore --interactive ignores <path>/--dir/--all; "
                "running the interactive flow instead",
                file=sys.stderr,
            )
        return _interactive_restore(repo, args)
    if args.id is None:
        print(
            "restore needs <id> (or use --interactive on a TTY)",
            file=sys.stderr,
        )
        return 2
    # Exactly one target scope. Without this, ``restore <id> f.txt --all`` parsed
    # cleanly and --all silently won, restoring everything and clobbering files
    # the user never named.
    scopes = sum(bool(x) for x in (args.path, args.dir, args.all))
    if scopes > 1:
        print(
            "restore: choose exactly one of <path>, --dir, or --all",
            file=sys.stderr,
        )
        return 2
    if args.all:
        if not args.yes and sys.stdin.isatty():
            try:
                ans = input(
                    "restore ALL files from {0}? this overwrites the work tree "
                    "[y/N] ".format(args.id)
                )
            except EOFError:
                ans = ""
            if ans.strip().lower() not in ("y", "yes"):
                print("aborted", file=sys.stderr)
                return 1
        elif not args.yes and not sys.stdin.isatty():
            print(
                "restore --all needs --yes in non-interactive mode",
                file=sys.stderr,
            )
            # Usage error -> exit 2 (the fatal/usage code), matching the
            # sibling usage errors above; 1 is reserved for benign skips.
            return 2
        status, msg = engine.restore_all(
            repo, args.id, no_backup, staged=args.staged
        )
    elif args.dir:
        status, msg = engine.restore_dir(
            repo, args.id, args.dir, no_backup, staged=args.staged
        )
    elif args.path:
        status, msg = engine.restore_file(
            repo, args.id, args.path, no_backup, staged=args.staged
        )
    else:
        print("restore needs <path>, --dir <subtree>, or --all", file=sys.stderr)
        return 2
    if status == 0:
        print(msg)
    else:
        print(msg, file=sys.stderr)
    return status


def cmd_prune(repo: str, args: argparse.Namespace) -> int:
    summary = engine.prune(
        repo,
        keep_generations=args.keep_generations,
        max_bytes=args.max_bytes,
        keep_hours=args.keep_hours,
        dry_run=args.dry_run,
        run_gc=not args.no_gc,
    )
    if summary.get("reason"):
        # Nothing was done (e.g. the repo lock was busy). Report on stderr and
        # signal the skip with a non-zero status instead of a misleading
        # "pruned 0 snapshot(s)" success.
        print("prune skipped: {0}".format(summary["reason"]), file=sys.stderr)
        return 1
    tag = "would prune" if summary.get("dry_run") else "pruned"
    # "loose object(s) packed/pruned": the count is a before/after loose-object
    # delta, which drops when git PACKS objects (still present), not only when it
    # prunes them -- so it is a rough housekeeping signal, not bytes reclaimed.
    print(
        "{0} {1} snapshot(s), kept {2}, {3} loose object(s) packed/pruned".format(
            tag,
            summary["pruned"],
            summary["kept"],
            summary["reclaimed_loose"],
        )
    )
    return 0


def _git_toplevel(path: str) -> "str | None":
    """Walk up from ``path`` to its git work-tree root, or None.

    Routes through gitutil.run_git so git's repo-selection env vars are scrubbed
    -- a leaked GIT_DIR/GIT_WORK_TREE would otherwise resolve the WRONG repo and
    the hook would snapshot it instead of the real target -- and the
    toplevel is decoded with surrogateescape so a non-UTF-8 repo path does not
    raise and silently skip the snapshot.
    """
    p = os.path.abspath(path)
    while p and p != os.path.dirname(p):
        r = gitutil.run_git(p, ["rev-parse", "--show-toplevel"], check=False)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "surrogateescape").strip()
        p = os.path.dirname(p)
    return None


_PATH_TOKEN_CAP = 64  # bound the rev-parse walks on the blocks-the-agent path


def _repos_from_command(command: str, cwd: str) -> "list[str]":
    """Find git repos touched by a destructive command (incl. repos other than cwd's).

    Tokenises with ``shlex`` so a quoted path that CONTAINS A SPACE stays a single
    token -- the old absolute-path regex split it and resolved the wrong repo.
    For every PATH-SHAPED token (absolute, ``~``-prefixed, or
    containing ``/`` -- e.g. ``./subrepo``, ``../sib``, ``a/b``: the relative
    nested-repo case the old absolute-only scan missed) it
    resolves the token against ``cwd`` and walks up to its git work-tree root.

    A bare-name token (no ``/``) is walked ONLY when it is an existing directory
    (a cheap ``os.path.isdir`` stat gates the ``git rev-parse`` walk), so a bare
    ``rm -rf subrepo`` still finds a NESTED repo while file / flag-operand
    arguments stay free -- catching that case without forking ``git rev-parse``
    once per argument. The walk count is also capped
    (the cwd fallback still protects the primary repo); a destructive command
    touching >64 distinct nested-repo paths is not a realistic shape.
    """
    seen: "list[str]" = []
    seen_set: "set[str]" = set()
    scanned: "set[str]" = set()

    def _add(root: "Optional[str]") -> None:
        if root and root not in seen_set:
            seen_set.add(root)
            seen.append(root)

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes etc.: fall back to the absolute-path regex so we at
        # least cover the no-space absolute case rather than nothing.
        import re

        tokens = re.findall(r'/[^\s;|&><"\'`\\\n]+', command)

    for tok in tokens:
        if len(scanned) >= _PATH_TOKEN_CAP:
            break
        if not tok:
            continue
        # A ``key=value`` operand hides its path AFTER the '=' (``dd of=/other/x``,
        # ``--directory=/x``); the bare token then looks relative and the target
        # repo is missed. Consider both the raw token (unless it is a
        # flag) and, when present, its post-'=' value as path candidates.
        cands: List[str] = []
        if not tok.startswith("-"):
            cands.append(tok)
        if "=" in tok:
            val = tok.split("=", 1)[1]
            if val:
                cands.append(val)
        for raw in cands:
            if raw.startswith("/") or raw.startswith("~"):
                cand = os.path.expanduser(raw)
            elif "/" in raw:
                cand = os.path.join(cwd, raw)
            elif os.path.isdir(os.path.join(cwd, raw)):
                # Bare-name token that is an existing directory: could be a NESTED
                # repo (``rm -rf subrepo``). The stat gate keeps non-dir args
                # (files, flag operands) from each forking a rev-parse walk.
                cand = os.path.join(cwd, raw)
            else:
                continue  # bare non-dir: covered by the cwd fallback
            key = cand.rstrip("/")
            if key in scanned:
                continue
            scanned.add(key)
            _add(_git_toplevel(key))

    # Fallback: walk up from cwd (covers bare-name targets like ``rm -rf notes``).
    _add(_git_toplevel(cwd))

    return seen


def _repos_from_file_path(file_path: str, cwd: str) -> "list[str]":
    """Find the git repo that contains ``file_path``.

    Used for Write/Edit tool events where the target is a single explicit path.
    Resolves the repo containing file_path, and also includes cwd's repo (deduped).
    """
    seen: "list[str]" = []
    seen_set: "set[str]" = set()

    for candidate in [file_path, cwd]:
        if not candidate:
            continue
        root = _git_toplevel(os.path.abspath(candidate))
        if root and root not in seen_set:
            seen_set.add(root)
            seen.append(root)
    return seen


def cmd_hook(repo: str, args: argparse.Namespace) -> int:
    """Read PreToolUse JSON on stdin; snapshot before Bash/Write/Edit tool calls.

    Always exits 0 (fail-open) so the safety net never blocks the agent. For a
    destructive Bash command we capture in THOROUGH mode (force-rehash, so an
    mtime-preserving in-place content swap right before the command is still
    captured); for read-only Bash and for Write/Edit/NotebookEdit we use the
    cheap conservative mode.

    Accepted limitation: the conservative path -- Write / Edit /
    NotebookEdit, read-only Bash, and any destructive Bash that the verb
    allowlist does not recognise (``python3 -c ...``, ``bash -c ...``, command
    substitution) -- reuses a cached blob when the file signature is unchanged
    (the signature includes ctime to defeat mtime-restoring tools; see
    :meth:`MtimeCache.signature`). The only residual is a content swap on a
    filesystem whose ctime resolution is too coarse to distinguish two same-size
    writes within one tick; force-rehashing the whole repo on every editor call
    is too costly to pay by default to close that last sliver.
    """
    # Read + parse stdin inside the guard: a None/closed/erroring stdin must not
    # escape and break the always-exit-0 contract.
    try:
        raw = sys.stdin.read() if sys.stdin is not None else ""
    except Exception:  # noqa: BLE001 - fail-open on any stdin error
        print("hook: could not read stdin, allowing", file=sys.stderr)
        return 0
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print("hook: bad JSON on stdin, allowing", file=sys.stderr)
        return 0
    # Valid JSON that is not an object (list / number / string) would crash
    # ``event.get(...)`` with AttributeError. Fail open instead (the docstring
    # promises we ALWAYS exit 0).
    if not isinstance(event, dict):
        print("hook: unexpected JSON shape on stdin, allowing", file=sys.stderr)
        return 0

    # Everything from here is wrapped so any unexpected shape / engine error
    # still exits 0 -- the safety net must never block the agent's tool call.
    try:
        tool_name = event.get("tool_name")
        if tool_name not in ("Bash", "Write", "Edit", "NotebookEdit"):
            return 0
        tool_input = event.get("tool_input") or {}
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            # A non-string / empty cwd must fall back, not crash a later
            # os.path.abspath and silently skip the protective snapshot via the
            # outer except.
            cwd = repo or os.getcwd()
        include_ignored = _include_ignored_from_env()

        # conservative=False => engine force-rehashes (the destructive moment).
        conservative = True
        if tool_name == "Bash":
            command = (
                tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            )
            if not isinstance(command, str):
                command = ""  # non-string command -> treat as empty
            label = "pre-bash: " + command.replace("\n", " ")
            # Thorough capture only when the command is actually destructive, so
            # read-only Bash stays cheap.
            destructive = looks_destructive(command)
            conservative = not destructive
            primary = os.path.abspath(cwd)
            if destructive:
                # Destructive: union cwd's repo with every repo the command's
                # path-shaped tokens (absolute, relative, or bare-name dirs)
                # resolve into, so ``rm -rf /other/repo/...``
                # from inside repoA snapshots repoB too.
                repos = _repos_from_command(command, cwd)
            elif gitutil.is_work_tree(primary):
                # Read-only: fast path -- snapshot only cwd's repo. Avoids the
                # per-token ``git rev-parse`` walks on every Bash call and avoids
                # littering UNRELATED repos that a read-only command merely reads
                # from.
                repos = [primary]
            else:
                repos = _repos_from_command(command, cwd)
        else:
            # Write / Edit: target is tool_input["file_path"]; NotebookEdit uses
            # "notebook_path".
            file_path = ""
            if isinstance(tool_input, dict):
                file_path = (
                    tool_input.get("file_path")
                    or tool_input.get("notebook_path")
                    or ""
                )
            if not isinstance(file_path, str):
                file_path = ""  # non-string path -> treat as absent
            label = "pre-{0}: {1}".format(tool_name.lower(), file_path)
            repos = _repos_from_file_path(file_path, cwd)

        if not repos:
            print(
                "hook: no git repo found (cwd={0})".format(cwd),
                file=sys.stderr,
            )
            return 0

        for target_repo in repos:
            # Per-repo isolation: a capture that raises for one repo (e.g. a
            # cross-repo destructive command touching repoA and repoB) must not
            # starve the remaining repos of their protective snapshot. Isolate
            # each iteration; the outer fail-open guard still backstops anything
            # else.
            try:
                res = engine.capture(
                    repo=target_repo,
                    label=label,
                    via="hook",
                    include_ignored=include_ignored,
                    conservative=conservative,
                )
            except Exception as exc:  # noqa: BLE001 - one repo must not block others
                print(
                    "hook: skip {0} (error, allowing): {1}".format(target_repo, exc),
                    file=sys.stderr,
                )
                continue
            if res.status == 0:
                print(
                    "hook: snapshot {0} ({1} files)".format(res.id, res.files),
                    file=sys.stderr,
                )
            else:
                print(
                    "hook: skip: {0}".format(res.reason), file=sys.stderr
                )
    except Exception as exc:  # noqa: BLE001 - fail-open on anything
        print("hook: error (allowing anyway): {0}".format(exc), file=sys.stderr)
    return 0  # always allow


def _nonneg_int(s: str) -> int:
    """argparse type: a non-negative int. A negative ``--limit`` previously
    slipped past ``limit > 0`` and silently listed everything."""
    v = int(s)
    if v < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return v


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-safepoint",
        description="work-tree snapshot safety net (MVP)",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s {0}".format(__version__)
    )
    parser.add_argument(
        "--repo", default=".", help="git repo to operate on (default: cwd)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="snapshot tracked+untracked work tree")
    p_snap.add_argument("--label", default=None, help="optional label")
    p_snap.add_argument("--force", action="store_true", help="ignore debounce")
    p_snap.add_argument(
        "--debounce", type=float, default=5.0,
        help="0 disables the unchanged-content skip; any non-zero value enables "
        "it (content-based, NOT a time window)",
    )
    p_snap.add_argument("--quiet", action="store_true", help="no stdout on success")
    p_snap.add_argument(
        "--via", choices=["hook", "preexec", "manual"], default="manual"
    )
    p_snap.add_argument(
        "--force-rehash",
        action="store_true",
        help="re-hash every file (ignore mtime cache)",
    )
    p_snap.add_argument(
        "--no-wait", action="store_true", help="skip if another snapshot holds the lock"
    )
    p_snap.add_argument(
        "--include-ignored",
        action="append",
        metavar="PATTERN",
        default=None,
        help="opt-in: also capture .gitignore'd paths matching PATTERN "
        "(e.g. output/ or '*.log'). Repeatable. Secret patterns "
        "(.env/*.key/*.pem/id_rsa/...) are ALWAYS excluded.",
    )

    p_list = sub.add_parser("list", help="list snapshots, newest first")
    p_list.add_argument("--json", action="store_true", help="JSON Lines output")
    p_list.add_argument(
        "--limit", type=_nonneg_int, default=0, help="max rows (0 = all)"
    )

    p_diff = sub.add_parser("diff", help="diff snapshots or snapshot vs work tree")
    p_diff.add_argument("id1", help="first snapshot id")
    p_diff.add_argument(
        "id2", nargs="?", default=None, help="second id (omit = work tree)"
    )
    p_diff.add_argument(
        "--path", default=None, help="restrict to one file (unified diff)"
    )
    p_diff.add_argument(
        "--staged",
        action="store_true",
        help="diff the snapshot's staged-index variant (git add -p content)",
    )

    p_rest = sub.add_parser("restore", help="restore from a snapshot")
    p_rest.add_argument(
        "id", nargs="?", default=None, help="snapshot id (or full ref)"
    )
    p_rest.add_argument("path", nargs="?", default=None, help="file to restore")
    p_rest.add_argument("--dir", default=None, help="restore a subtree")
    p_rest.add_argument("--all", action="store_true", help="restore everything")
    p_rest.add_argument(
        "--interactive",
        action="store_true",
        help="TTY flow: list -> pick -> diff -> confirm (restore --interactive)",
    )
    p_rest.add_argument(
        "--no-backup", action="store_true", help="do not stash overwritten files"
    )
    p_rest.add_argument(
        "--yes", action="store_true", help="skip confirmation for --all"
    )
    p_rest.add_argument(
        "--staged",
        action="store_true",
        help="restore the snapshot's staged-index variant (git add -p content)",
    )

    p_prune = sub.add_parser("prune", help="apply retention policy + gc")
    p_prune.add_argument("--keep-generations", type=int, default=50)
    p_prune.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    p_prune.add_argument("--keep-hours", type=int, default=72)
    p_prune.add_argument("--dry-run", action="store_true")
    p_prune.add_argument("--no-gc", action="store_true", help="drop refs but skip gc")

    sub.add_parser("hook", help="PreToolUse hook entry (reads JSON on stdin)")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Non-UTF-8 filenames/labels are decoded with surrogateescape (gitutil) and
    # then flow into stdout print()s (diff / list / restore). Under a normal UTF-8
    # developer locale stdout uses errors='strict', where a lone surrogate code
    # point raises UnicodeEncodeError mid-command and crashes the recovery tool.
    # Render such bytes as \xNN escapes instead. stderr already
    # defaults to 'backslashreplace' on CPython, so only stdout needs this; guard
    # for a replaced / captured stdout that cannot be reconfigured.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    repo = os.path.abspath(args.repo)

    # `hook` resolves its own repo from the JSON cwd, so don't gate it here.
    if args.cmd != "hook":
        if not gitutil.is_work_tree(repo):
            print("not a git work tree: {0}".format(repo), file=sys.stderr)
            return 2
        # Normalize --repo to the work-tree ROOT (the hook/preexec convention).
        # A subdirectory would otherwise capture a subtree-only snapshot into the
        # repo-wide refs/snapshots namespace, silently mixing partial snapshots
        # with full ones. Warn when we redirect.
        toplevel = _git_toplevel(repo)
        if toplevel:
            if os.path.realpath(toplevel) != os.path.realpath(repo):
                print(
                    "note: --repo {0} is below the work-tree root; operating on "
                    "{1}".format(repo, toplevel),
                    file=sys.stderr,
                )
            repo = toplevel

    dispatch = {
        "snapshot": cmd_snapshot,
        "list": cmd_list,
        "diff": cmd_diff,
        "restore": cmd_restore,
        "prune": cmd_prune,
        "hook": cmd_hook,
    }
    handler = dispatch.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 1
    try:
        return handler(repo, args)
    except KeyboardInterrupt:
        # restore reports its own partial progress before re-raising;
        # here we just exit cleanly instead of dumping a traceback.
        print("\naborted (interrupted)", file=sys.stderr)
        return 130
    except subprocess.TimeoutExpired as exc:
        # A git call exceeded its timeout. Never block: report and
        # exit. A timeout is an operation FAILURE, not a benign skip, so exit 2.
        # (The hook path swallows this itself and still exits 0.)
        print("git timed out, aborted: {0}".format(exc), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        # An unexpected git plumbing failure (check=True). Report the git stderr
        # cleanly instead of dumping a Python traceback.
        detail = ""
        if exc.stderr:
            detail = exc.stderr.decode("utf-8", "replace").strip()
        print("git command failed: {0}".format(detail or exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
