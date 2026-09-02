"""Canonical `python3 -m llmwiki` entry point, usable without installing the package."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
