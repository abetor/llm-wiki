"""Fail-closed TOML loading where unknown fields are errors rather than ignored input.

Vendored from tool-template and derived from a scheduler configuration loader. Silently accepting
a misspelled field creates configuration that appears applied but has no effect, allowing defects
to survive for months. Unknown fields therefore fail loading.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def load_toml(path: str | Path, *, known: set[str],
              required: frozenset[str] | set[str] = frozenset()) -> dict:
    """Load TOML, validate field names, and return the raw dictionary.

    Raise ValueError for unknown or missing required fields. Invalid syntax arrives as the
    TOMLDecodeError subclass. Defaults and type checks belong to the caller; this layer guarantees
    only that fields match the declared contract.
    """
    path = Path(path)
    raw = tomllib.loads(path.read_text("utf-8"))
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"{path.name}: unknown fields {sorted(unknown)}")
    missing = set(required) - set(raw)
    if missing:
        raise ValueError(f"{path.name}: missing required fields {sorted(missing)}")
    return raw
