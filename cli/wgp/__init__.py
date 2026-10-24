#!/usr/bin/env python3

# File: cli/wgp/__init__.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-10-24
# Description: https://pixeldrain.com/u/cdc5zCYR
# License: MIT

"""wgp - command line client for wgetpanel (API v1)."""

from .wgp import __version__, main

__all__ = ["__version__", "main"]