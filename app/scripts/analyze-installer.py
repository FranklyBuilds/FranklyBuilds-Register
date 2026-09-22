#!/usr/bin/env python3
"""Compatibility entry point for the installer archive analyzer.

The importable implementation lives in :mod:`analyze_installer`; this file
keeps the command name readable when invoked directly from PowerShell.
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("analyze_installer.py")), run_name="__main__")
