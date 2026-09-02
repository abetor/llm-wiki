"""Harness port for running LLM work through interchangeable subscription-backed agent CLIs.

Vendored from tool-template and derived through production-tested orchestration and research
adapters. This repository owns the file. Do not change CLI flags or classification patterns
without a live verification run.

The minimum harness contract is a headless one-shot process with prompt and cwd, final text on
stdout, and a distinguishable failure. Everything else is a capability flag. The wrapper emulates
features absent from a harness instead of pretending the harness supports them. This module also
classifies stop states as done, quota, transient, or fatal. No machine-readable quota endpoint
exists, so classification relies on output patterns.
"""
from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# These patterns are common to CLI harnesses; an adapter adds its own through quota_patterns().
#
# Class boundary from a 2026-08-10 production defect. Previously 429 and "rate limit" shared
# _QUOTA with exhausted subscription budget. A burst of local parallelism, two runs with pools of
# three, produced an ordinary 429. The classifier called it quota, the CLI returned 75, and the
# external driver removed all remaining work even though waiting a few seconds was sufficient.
# Separate classes by their remedy, not by a shared label.
#
# _QUOTA means subscription budget is exhausted and only a reset window, often hours or days, can
# fix it. Match words about budget itself rather than request rate. Observed forms include:
# claude - "Claude usage limit reached ... resets at", "You've hit your weekly limit",
# "5-hour limit reached"; codex - "You've hit your usage limit", "out of credits".
# `(?<!rate[ -])\blimit (reached|exceeded)` encodes the important boundary: "usage limit exceeded"
# is quota, while "rate limit exceeded", the usual throttling phrase, is not.
_QUOTA = re.compile(r"usage limit|weekly limit|session limit|5-hour|out of credits|quota"
                    r"|(?:hit|reached) your [^.\n]{0,24}limit"
                    r"|(?<!rate[ -])\blimit (?:reached|exceeded)", re.I)
# _TRANSIENT is fixed by retrying after seconds or minutes. It includes burst throttling such as
# 429, "too many requests", and "rate limit". Both harnesses document this separately from quota
# and retry it internally with backoff. If it reaches this layer after their attempts, waiting
# again is correct, not stopping with a checkpoint. For mixed messages such as "429: usage limit
# reached", classify checks quota first. This is deliberate: repeatedly hitting real quota is
# worse than an extra wait, and budget wording is more specific than the generic 429 code.
_TRANSIENT = re.compile(r"\b(?:429|500|502|503|529)\b|too many requests|rate.?limit"
                        r"|overloaded|timed?.?out|connection|"
                        r"temporarily|try again|ECONNRESET|EAI_AGAIN", re.I)

# Map stop states to the shared CLI exit-code contract described in cli.py.
STOP_TO_EXIT = {"done": 0, "quota": 75, "transient": 111, "fatal": 1}


@dataclass
class Capabilities:
    """Capabilities supported natively by a harness; the wrapper emulates the rest."""
    json_events: bool
    schema_output: bool
    native_resume: bool
    subagents: bool
    mcp: bool


@dataclass
class RunResult:
    ok: bool
    text: str
    exit_code: int
    session_id: Optional[str] = None
    stop: str = "done"                 # done | quota | transient | fatal
    cost_usd: Optional[float] = None
    raw: dict = field(default_factory=dict)
    stderr: str = ""


class HarnessAdapter(ABC):
    """Per-CLI knowledge for command construction and output parsing; do not override shared run()."""
    name: str = "harness"

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def build_cmd(self, prompt: str, *, model: Optional[str] = None,
                  effort: Optional[str] = None, resume_session_id: Optional[str] = None,
                  schema_path: Optional[str] = None, system_prompt_path: Optional[str] = None,
                  allowed_tools: Optional[str] = None) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, exit_code: int) -> RunResult: ...

    def quota_patterns(self) -> Optional[re.Pattern]:
        """Return per-CLI quota signals in addition to the common patterns."""
        return None

    def classify(self, result: RunResult) -> str:
        """Classify a result as done, quota, transient, or fatal in contractual order.

        1. Check quota even on successful output because a harness may exit zero while saying
           "you have hit your limit". Quota also wins mixed messages such as
           "429: usage limit reached" as explained above.
        2. A successful result is done. Throttling is intentionally checked after this step: a 429
           with exit zero means the harness retried and ultimately succeeded. Also, result.text may
           contain externally collected material, where a page discussing "rate limits" must not
           turn a successful run into a transient failure.
        3. On failed calls, classify burst throttling and network faults as transient.

        Raw blob keys are part of the port contract: subtype/api_error_status for single-JSON
        responses and error for JSONL error events.
        """
        blob = f"{result.text}\n{result.stderr}\n{result.raw.get('subtype', '')}\n" \
               f"{result.raw.get('api_error_status', '')}\n{result.raw.get('error', '')}"
        extra = self.quota_patterns()
        if _QUOTA.search(blob) or (extra and extra.search(blob)):
            return "quota"
        if result.ok:
            return "done"
        if _TRANSIENT.search(blob):
            return "transient"
        return "fatal"

    def run(self, prompt: str, *, cwd: str, timeout: Optional[float] = None,
            **build_kw) -> RunResult:
        """Run one harness call; this is the only place that spawns a process."""
        cmd = self.build_cmd(prompt, **build_kw)
        try:
            # DEVNULL fixes a production hang: codex exec reads inherited stdin until EOF while
            # reporting "Reading additional input from stdin...".
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return RunResult(ok=False, text=out, exit_code=-1,
                             stderr="wall-timeout", stop="transient")
        res = self.parse_output(proc.stdout, proc.returncode)
        res.stderr = proc.stderr or ""
        res.stop = self.classify(res)
        if res.stop != "done":
            res.ok = False
        return res
