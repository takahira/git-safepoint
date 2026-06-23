#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone entry point: ``python3 git_safepoint.py --repo <path> <subcommand>``.

Adds this file's directory to sys.path so the ``git_safepoint`` package imports even
when invoked by absolute path (e.g. from a Claude Code hook command).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from git_safepoint.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
