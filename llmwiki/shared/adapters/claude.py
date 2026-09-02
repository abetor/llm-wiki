"""Claude Code CLI adapter vendored from tool-template.

Its flag behavior was verified in live orchestration and research runs. Do not change it without
another live verification.

`claude -p --output-format json [...] -- "prompt"` -> stdout single-json:
{result, session_id, total_cost_usd, is_error, subtype, ...}.

Verified facts:
- the schema is passed inline as a string through --json-schema, not as a path;
- `--` before the prompt is required because --allowedTools greedily consumes the next positional
  argument and otherwise produces "Input must be provided";
- resume uses the --resume <session_id> flag;
- `claude -p` normally uses subscription quota, but issue #43333 reports silent API billing, so
  usage must be logged and compared with expectations.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Capabilities, HarnessAdapter, RunResult

# The default allowlist permits web and reads but not project writes. This physically enforces
# read-only behavior. A writing agent must receive its own allowed_tools.
READ_ONLY_TOOLS = "WebSearch WebFetch Read Grep Glob LS"


class ClaudeAdapter(HarnessAdapter):
    name = "claude"

    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def capabilities(self) -> Capabilities:
        return Capabilities(json_events=True, schema_output=True, native_resume=True,
                            subagents=True, mcp=True)

    def build_cmd(self, prompt, *, model=None, effort=None, resume_session_id=None,
                  schema_path=None, system_prompt_path=None, allowed_tools=None):
        cmd = [self.binary, "-p", "--output-format", "json",
               "--permission-mode", "default"]
        if model:
            cmd += ["--model", model]
        # Under mode=default, a tool outside the allowlist cannot run in noninteractive -p because
        # nobody can approve it. The allowlist therefore defines the call's capability boundary.
        cmd += ["--allowedTools", allowed_tools or READ_ONLY_TOOLS]
        if effort:
            cmd += ["--effort", effort]
        if system_prompt_path:
            # Use a separate system-prompt file without overwriting the project's CLAUDE.md.
            cmd += ["--append-system-prompt-file", system_prompt_path]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        if schema_path:
            schema = Path(schema_path).read_text("utf-8").strip()
            if not schema:
                raise ValueError(f"empty schema {schema_path}: refusing an unconstrained call")
            cmd += ["--json-schema", schema]  # Claude expects an inline schema; Codex expects a path.
        cmd += ["--", prompt]
        return cmd

    def parse_output(self, stdout, exit_code) -> RunResult:
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            # Preserve non-JSON output from a crash before formatting.
            return RunResult(ok=exit_code == 0 and bool(stdout.strip()),
                             text=stdout, exit_code=exit_code)
        ok = exit_code == 0 and not data.get("is_error", False)
        return RunResult(ok=ok, text=data.get("result") or "", exit_code=exit_code,
                         session_id=data.get("session_id"),
                         cost_usd=data.get("total_cost_usd"), raw=data)

    def quota_patterns(self) -> re.Pattern:
        # Observed subtype values in JSON responses when subscription or API limits are reached.
        return re.compile(r"error_max_turns|resets at|upgrade to|claude usage", re.I)
