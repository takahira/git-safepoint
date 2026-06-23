#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Allow ``python3 -m git_safepoint ...``."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
