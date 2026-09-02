"""ArgumentParser aligned with the workspace exit-code contract: usage errors return 1.

Stock argparse ends invalid argument parsing through sys.exit(2), while this CLI defines all
failures as EXIT_FAIL=1. The subclass changes only the exit code, preserving usage and the stderr
message. Subparsers inherit the class automatically because add_subparsers uses the parent's type,
so subcommand errors also return 1.

Vendored from tool-template.
"""
from __future__ import annotations

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    """Behave like argparse.ArgumentParser, but exit with 1 for usage errors."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")
