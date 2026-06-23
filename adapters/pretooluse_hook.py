#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code PreToolUse hook for git-safepoint.

Register this as a PreToolUse hook with matcher ``Bash`` (and optionally
``Write``/``Edit``). Claude Code feeds the tool call as JSON on stdin::

    {"hook_event_name":"PreToolUse","tool_name":"Bash",
     "tool_input":{"command":"rm -rf notes"},"cwd":"/abs/repo"}

We snapshot the work tree before EVERY matched tool call and exit 0 to allow the
call to proceed (fail-open: the safety net never blocks the agent). Read-only
Bash and Write/Edit use the cheap conservative capture; a destructive Bash
command triggers a thorough force-rehash capture. The real Claude Code session
is not required to exercise this -- pipe the same JSON shape on stdin and it
behaves identically (see tests).

Fail-open is enforced here too: even an import error (broken / partial install)
exits 0 rather than crashing the agent's tool call with a traceback.
"""
import os
import sys

if __name__ == "__main__":
    try:
        sys.path.insert(
            0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        from git_safepoint.cli import main

        # Delegate to the `hook` subcommand. --repo is a fallback; the real repo
        # is read from the JSON `cwd`. Use the glued ``--repo=<value>`` form so a
        # CLAUDE_PROJECT_DIR that begins with '-' is not mis-parsed by argparse as
        # an option (which would raise SystemExit(2) and BLOCK the agent's tool
        # call -- a fail-open violation).
        repo = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        sys.exit(main(["--repo=" + repo, "hook"]))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - fail-open on anything
        sys.stderr.write("git-safepoint hook: error (allowing): {0}\n".format(exc))
        sys.exit(0)
