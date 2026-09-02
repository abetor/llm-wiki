"""Harness port providing agent CLI adapters and a factory selected by name."""
from .base import Capabilities, HarnessAdapter, RunResult, STOP_TO_EXIT
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def make_adapter(name: str) -> HarnessAdapter:
    """Return an adapter by harness name; unknown names raise ValueError with known choices."""
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise ValueError(f"unknown harness {name!r} (available: {sorted(ADAPTERS)})") from None


__all__ = ["ADAPTERS", "Capabilities", "ClaudeAdapter", "CodexAdapter",
           "HarnessAdapter", "RunResult", "STOP_TO_EXIT", "make_adapter"]
