# git-safepoint

**Local-first work-tree snapshot safety net for AI-assisted coding.**

Captures tracked + untracked files before every destructive command, and lets
you restore just the file you lost. No cloud. No accounts. Standard library
only. Touches neither your index nor HEAD.

[![PyPI](https://img.shields.io/pypi/v/git-safepoint.svg)](https://pypi.org/project/git-safepoint/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/takahira/git-safepoint)

---

## The problem

AI coding tools have a structural blind spot: **bash/terminal commands and untracked files**.

- Claude Code [`/rewind`](https://code.claude.com/docs/en/checkpointing) explicitly documents it cannot restore bash changes (`rm`, `mv`, `cp`, …)
- [copilot-cli #1675](https://github.com/github/copilot-cli/issues/1675) (Feb 2026): checkpoint restore deleted ~1 GB of untracked files via `git clean -fd`
- Replit (Jul 2025), Gemini CLI (Jul 2025), PocketOS (Apr 2026) — same pattern, different tools

`git reflog` won't help. These files were never staged.

git-safepoint fills that gap.

---

## What makes it different

<!-- markdownlint-disable MD060 -->
| Feature                         | git-safepoint | mrq¹            | ckpt²               | Re_gent³            | Native AI⁴         |
|---------------------------------|---------|-----------------|---------------------|---------------------|--------------------|
| Local-only (no cloud upload)    | Yes     | No (cloud only) | Yes                 | Yes                 | Yes                |
| Captures untracked files        | Yes     | Partial         | Unverified          | Yes                 | No                 |
| Covers manual bash / ext tools  | Yes     | Yes (fs-watch)  | Yes                 | No (agent-hook)     | No                 |
| File-level selective restore    | Yes     | No (whole snap) | Partial (gen only)  | No (not impl)       | No (session only)  |
| Free OSS                        | Yes     | No ($9–29/mo)   | Yes                 | Yes                 | Yes                |
<!-- markdownlint-enable MD060 -->

¹ [mrq](https://getmrq.com) — commercial cloud snapshot service  
² [ckpt](https://github.com/mohshomis/ckpt) — OSS, TypeScript/Node  
³ [Re_gent](https://github.com/regent-vcs/re_gent) — OSS, Go, agent-hook only  
⁴ Claude Code `/rewind`, Cursor checkpoints, Gemini CLI, Codex CLI

git-safepoint is the only tool we're aware of that satisfies all four simultaneously: **local OSS × true untracked-safe × covers manual bash × snapshot-id / file-level restore**.

---

## Install

Zero runtime dependencies — standard library only, Python 3.9+.

```sh
# Recommended: install from PyPI (pipx keeps it isolated)
pipx install git-safepoint
#   ... or:  pip install git-safepoint

# Bleeding edge, straight from GitHub:
pipx install git+https://github.com/takahira/git-safepoint

# Or run from a clone without installing (also gives you the adapters/ hooks)
git clone https://github.com/takahira/git-safepoint
cd git-safepoint
```

After a pip/pipx install the CLI is `git-safepoint` (or `python3 -m git_safepoint`).
From a clone it is `python3 git_safepoint.py`. The Claude Code / zsh hook adapters
under `adapters/` ship with the clone (and the sdist) — use the clone if you want
them.

## Quick start

```sh
# Snapshot the current work tree (tracked + untracked; secrets auto-excluded)
git-safepoint --repo /path/to/your/repo snapshot --label "before refactor"

# Also capture .gitignore'd build artifacts (secrets stay excluded even here)
git-safepoint --repo . snapshot --include-ignored 'output/' --include-ignored '*.log'

# List snapshots (newest first)
git-safepoint --repo . list
git-safepoint --repo . list --json

# Diff between two snapshots (or vs current work tree)
git-safepoint --repo . diff <id1> [<id2>] [--path FILE]

# Restore a single file
git-safepoint --repo . restore <id> path/to/lost-file.txt

# Restore a subtree or everything
git-safepoint --repo . restore <id> --dir notes/
git-safepoint --repo . restore <id> --all --yes

# Interactive restore (TTY: list → diff → confirm)
git-safepoint --repo . restore --interactive

# Recover a partially-staged version (git add -p content lost to reset --hard)
git-safepoint --repo . restore <id> path/to/file --staged

# GC / retention
git-safepoint --repo . prune --keep-generations 50 --dry-run
```

From a clone (no install), replace `git-safepoint` with `python3 git_safepoint.py`.

---

## Claude Code hook (auto-capture before every tool call)

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python3 /abs/path/to/git-safepoint/adapters/pretooluse_hook.py"}]
      },
      {
        "matcher": "Write",
        "hooks": [{"type": "command", "command": "python3 /abs/path/to/git-safepoint/adapters/pretooluse_hook.py"}]
      },
      {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "python3 /abs/path/to/git-safepoint/adapters/pretooluse_hook.py"}]
      },
      {
        "matcher": "NotebookEdit",
        "hooks": [{"type": "command", "command": "python3 /abs/path/to/git-safepoint/adapters/pretooluse_hook.py"}]
      }
    ]
  }
}
```

Reload your Claude Code window. The hook fires before every Bash, Write, Edit, and NotebookEdit call:

- **Conservative mode**: skips if nothing changed (mtime check + tree SHA dedup), so it's low-overhead on read-only calls
- **Fail-open**: always exits 0 — the safety net never blocks the agent
- Detects git repos from command paths when `cwd` is outside any repo

Hooks take no CLI flags. To **also capture `.gitignore`'d build artifacts** through them, set `GIT_SAFEPOINT_INCLUDE_IGNORED` — see [Configuration](#configuration-environment-variables). (Secrets stay excluded regardless.)

Live-verified with the Claude Code VSCode extension (June 2026).

---

## zsh preexec (auto-capture before terminal commands)

```sh
export GIT_SAFEPOINT_PY=/abs/path/to/git-safepoint/git_safepoint.py
source /abs/path/to/git-safepoint/adapters/git-safepoint-preexec.zsh
```

Snapshots before destructive shell commands (`rm`, `mv`, `git reset --hard`, etc.).

---

## Configuration (environment variables)

No config file — these optional environment variables tune behaviour. The first
is the one most people want: **the hook and `preexec` adapters take no CLI flags,
so capturing `.gitignore`'d build artifacts through them is opt-in here.**

| Variable | Default | Effect |
| --- | --- | --- |
| `GIT_SAFEPOINT_INCLUDE_IGNORED` | *(unset)* | `:`- (or `,`-) separated allow-list of otherwise-`.gitignore`'d paths to **also** capture through the **hook / preexec** path — e.g. `'output/:dist/'`. The secret floor still applies, so credentials stay excluded even here. (From the CLI, use `--include-ignored` instead.) |
| `GIT_SAFEPOINT_GIT_TIMEOUT` | `120` | Timeout (seconds) for each git invocation. |
| `GIT_SAFEPOINT_GC_TIMEOUT` | `600` | Timeout (seconds) for the `prune` gc step. |
| `GIT_SAFEPOINT_RESTORE_TIMEOUT` | `3600` | Timeout (seconds) for restore git operations. |
| `GIT_SAFEPOINT_PY` | *(unset)* | Absolute path to `git_safepoint.py` for the zsh `preexec` adapter (see [zsh preexec](#zsh-preexec-auto-capture-before-terminal-commands)). |

> **`.gitignore`'d build artifacts not being snapshotted?** That's by design — set
> `GIT_SAFEPOINT_INCLUDE_IGNORED` (hook / preexec) or pass `--include-ignored` (CLI).
> Secrets are never captured either way.

---

## How it works

git-safepoint uses git plumbing only — no diffs, no stash, no index changes:

1. `git ls-files --cached --others --exclude-standard` enumerates tracked + untracked
2. Files are stored with `git hash-object -w` into the repo's object store (batch mode: 500 files/fork)
3. A **private index** (separate from yours) builds a tree with `git write-tree`
4. A shadow commit lands at `refs/snapshots/<timestamp-seq-pid>` — HEAD and your index are untouched
5. If the **staged index** differs from both the work tree and HEAD (a `git add -p` / stage-then-edit state that `reset --hard` would otherwise destroy), that index is captured as the snapshot commit's parent — `list` marks it `+staged`, and `restore --staged <id> …` / `diff --staged …` reach it. Built from a copy of the index, so the real index is never touched.

All state lives under `.git/snap/` and never touches your work tree. With
`git worktree`, the `lock` and `seq` are shared on the common `.git` (so
captures across linked worktrees are serialised and IDs stay monotonic); the
mtime cache is per worktree:

- `mtime-cache.json` — incremental hash cache; only rehashes changed files (per worktree)
- `seq` — monotonic counter for collision-free IDs across concurrent processes and linked worktrees
- `lock` — per-repo `flock` (shared across linked worktrees) so parallel hook fires don't corrupt

**Secrets** (`.env`, `*.pem`, `id_rsa`, `*.key`, `*firebase-adminsdk*.json`, etc.) are excluded from snapshots — **for untracked files**. The floor's job is to keep an untracked secret out of the object store; a file already **tracked by git** is exempt (its blob is already committed, so snapshotting it leaks nothing and excluding it would only leave it unprotected). Editor/merge backup & swap copies of a recognised secret (`.env~`, `id_rsa.bak`, `server.pem.swp`, `#.env#`) are excluded too. The exclusion list is **name-based** (a conservative floor): a credential with an unrecognizable name — e.g. a randomly-named cloud service-account key — won't be auto-excluded, so keep it `.gitignore`'d or outside the repo. `.gitignore`'d files are excluded by default (the floor still applies even with `--include-ignored`). Snapshots survive `git clean -fdx`. Destroyed by `rm -rf .git` (same single point of failure as git itself).

---

## Performance (measured on macOS, Python 3.14, SSD)

| Files  | Cold (first capture) | Incremental (no change) | Incremental (1 file changed) |
|-------:|---------------------:|------------------------:|-----------------------------:|
|  1,000 | ~0.6 s               | ~90–100 ms              | ~100 ms                      |
|  5,000 | ~2.8 s               | ~150–250 ms             | ~150–250 ms                  |
| 10,000 | ~6.5 s               | ~230–410 ms             | ~230–460 ms                  |

Cold uses batch hashing (500 files/fork), reducing it from ~100 s to ~6.5 s vs. the naive one-process-per-file approach. Incremental uses an mtime+size+inode+exec-bit+ctime+type signature cache to skip unchanged files.

---

## Retention / GC

```sh
# Keep last 50 snapshots, max 512 MiB total, max 72 hours
git-safepoint --repo . prune --keep-generations 50 --max-bytes 536870912 --keep-hours 72

# Dry run first
git-safepoint --repo . prune --dry-run
```

The most recent snapshot is always preserved regardless of **any** retention
limit (size, generations, or age) — even `--keep-generations 0` or when every
snapshot is older than `--keep-hours`.

`prune` runs `git gc` with the prune/reflog grace **pinned** on the command line
to specific safe values — reflog expiry is pinned to `never` in both directions
(`gc.reflogExpire=never`, `gc.reflogExpireUnreachable=never`), and
`gc.pruneExpire=2.weeks.ago` — so an aggressive user `gc.*` config cannot
force-prune *regardless of any aggressive `gc.pruneExpire` in your git config*. Your own unreachable-but-recoverable objects (dropped stashes,
pre-reset commits, reflog history) are not collaterally collected. Dropped
snapshot objects are reclaimed on git's normal grace schedule. Use `--no-gc` to
drop refs without any gc.

---

## Known limitations

The hook's destructive-command detection uses a verb-allowlist approach optimized for zero false positives. It misses destruction hidden inside arguments:

- `find . -exec rm {} \;` — verb is `find`, not `rm`
- `` echo `rm -rf x` `` — verb is `echo`
- `(rm -rf x)` — leading token is `(`
- `python3 -c "open('f','w').write(...)"` — verb is `python3`

The git-subcommand allowlist (`checkout`/`switch`/`restore`/`reset`/`clean`/`rm`/`stash`, plus `branch -D`) is deliberately narrow: recovery/abort subcommands that can touch the work tree — `rebase`/`merge`/`am`/`cherry-pick --abort`, `read-tree -u`, `checkout-index -f`, `worktree remove` — are **not** individually thorough-mode triggers. Most refuse to run with conflicting uncommitted changes (so they don't silently destroy unsaved work), and any residual case is covered by conservative mode below; the trade-off keeps the false-positive rate near zero.

Conservative mode (used by the Claude Code hook) covers most of these by snapshotting on every tool call rather than only on destructive ones. For an *undetected* destructive command above, the conservative path still captures content changes (the file signature includes mtime, size, inode, exec-bit and **ctime**, so even an external `tar -x` / `rsync --times` / `cp -p` that restores mtime is caught). The remaining sliver is a same-size in-place content swap on a filesystem whose ctime resolution is too coarse to separate two writes in one tick; for those, only a destructive command the allowlist *does* recognise force-rehashes. The zsh preexec path has no conservative fallback, so the verb-allowlist gaps apply there in full.

Control-flow / compound bodies *are* detected (`if …; then rm …; fi`, `for/while … do rm …`, `{ rm …; }`). One repo-resolution gap remains: a **bare-name** target reached only after a `cd` in the same command line — `cd sub && rm -rf nestedrepo` — resolves `nestedrepo` against the original cwd, so a *separate nested git repo* at `sub/nestedrepo` is not found (the outer repo is still snapshotted). Use a path that contains a `/` (`rm -rf sub/nestedrepo`) and it is found.

**Batch restore is per-file atomic, not all-or-nothing.** Each file lands via `os.replace` (a crash never leaves a half-written file), but `restore --all` / `--dir` over many files is not a single transaction. If interrupted (Ctrl-C / kill) midway the work tree is left part-restored; git-safepoint prints how many files it restored so you can re-run the same restore to finish (restored files are idempotent and any overwritten originals are saved under `.snap-bak/`).

**Capture is not a point-in-time snapshot.** Files are stat'd and then hashed in separate steps, and the per-repo lock only excludes other git-safepoint processes — not your editor or build. If an external writer changes a file *during* a capture, that file may be stored with a slightly torn view (new bytes against the pre-write mode); it is never corrupt git data, and the next capture re-hashes it. Submodules record only their pinned commit (the pin is restored manually, not the submodule work tree); in **conservative** mode a submodule-only HEAD change (no change to the superproject's own files) is skipped before any tree is built, so the pin is captured best-effort there — a destructive command still force-captures the live pin.

**Reserved work-tree names.** git-safepoint never captures its own restore artifacts: the `.snap-bak/` directory (pre-overwrite backups) and any path ending in `.snap-restore-tmp` (in-flight restore temp files). A user file that happens to use those names is excluded from snapshots — avoid them.

**Concurrency / `.git` on a network filesystem.** The per-repo lock uses
`fcntl.flock`, which is reliable on a local filesystem. On some NFS mounts
(`nolock`/`local_lock`) and overlay/network filesystems `flock` may not actually
serialize across hosts; the lock then degrades silently. The collision-free ID
mint (create-only ref + retry) still prevents corrupt refs, and the mtime cache
is a pure optimization, so the worst realistic outcome is duplicated work / a
cold re-hash — not ref-store corruption. Keep `.git` on a local filesystem for
guaranteed serialization.

**Local metadata leak.** Two minor, local-only caveats: a snapshot's commit
message records the (truncated) triggering command **verbatim — there is no
redaction**, so avoid putting a secret directly on a destructive command line
(e.g. `... --token=…`); and a snapshot of a symlink
stores the link's *target path string* (never the secret's bytes). A normal push
(`refs/heads` / `refs/tags`) does not transfer `refs/snapshots/`, so this stays
on your machine by default — but `git push --mirror`, `git clone --mirror`, and
`git bundle --all` *do* carry the snapshot refs and their objects, so if you
mirror the repo as a backup, prune first or keep the destination inside your
trust boundary. Either way, if a command line or a symlink target itself contains
a secret, that *string* lives in your local object store until the snapshot is
pruned.

---

## Running tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
# → 305 tests pass (macOS / Linux; 2 non-UTF-8-name tests skip on macOS)
```

---

## Status

MVP — full test suite passing (see [Running tests](#running-tests)), live in Claude Code sessions.

**Implemented**: snapshot engine (tracked + untracked + opt-in .gitignore'd), secret exclusion (tracked files exempt), incremental capture, debounce + tree-SHA dedup, collision-free IDs across concurrent processes, staged-index variant capture (`restore --staged`), single-file / subtree / all / interactive restore, diff, GC/prune, Claude Code PreToolUse hook (conservative mode, live-verified), zsh preexec adapter.

**Not yet**: daemon mode (fswatch / kqueue), interactive TUI, off-`.git` mirror.

---

## Requirements

- Python 3.9+
- git 2.x for snapshot / restore; **git ≥ 2.25** for the snapshot-vs-work-tree
  `diff` / interactive-restore preview (it uses `git add --pathspec-file-nul`;
  on older git that one feature reports an error instead of a wrong empty diff)
- macOS or Linux (Windows: untested)

---

## License

MIT
