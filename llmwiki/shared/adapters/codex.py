"""OpenAI Codex CLI adapter vendored from tool-template.

Its flag behavior was verified in live orchestration and research runs with Codex 0.137. Do not
change it without another live verification.

`codex exec [resume <id>] --json -c approval_policy=never [--sandbox ...]
[--model M] [-c model_reasoning_effort=E] [-c model_instructions_file=PATH]
[--output-schema PATH] "prompt"` -> stdout is a JSONL event stream
(thread.started/thread_id, item.completed/agent_message, turn.completed/usage).

Verified facts, including three production fixes first observed in the research adapter:
- base.run must use stdin=DEVNULL because codex exec hangs on inherited stdin;
- --skip-git-repo-check is required when a tool working directory is not a Git repository, or Codex
  rejects it with "Not inside a trusted directory";
- resume is the `exec resume <id>` subcommand and does not accept --sandbox. Supply the same mode
  through -c sandbox_mode= instead;
- approval_policy and effort are configuration overrides through -c, not exec flags;
- --output-schema receives a file path, unlike Claude's inline schema;
- schema and resume are incompatible under codex#14343, closed as not planned;
- a ChatGPT subscription rejects Claude-family model names with 400 invalid_request, so omit
  --model and use the account default;
- Codex reports tokens, not currency. cost_usd is None and usage remains in raw.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Capabilities, HarnessAdapter, RunResult

# Claude-family aliases include full claude-* names and short forms accepted by the Claude CLI.
_CLAUDE_ALIASES = {"sonnet", "opus", "haiku"}


def _claude_family(model: str) -> bool:
    m = model.lower()
    return m.startswith("claude") or m in _CLAUDE_ALIASES


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def __init__(self, binary: str = "codex"):
        self.binary = binary

    def capabilities(self) -> Capabilities:
        # subagents are TOML agents in .codex/agents; MCP covers both client and server modes.
        return Capabilities(json_events=True, schema_output=True, native_resume=True,
                            subagents=True, mcp=True)

    def build_cmd(self, prompt, *, model=None, effort=None, resume_session_id=None,
                  schema_path=None, system_prompt_path=None, allowed_tools=None):
        # Ignore allowed_tools because Codex has no per-tool allowlist; sandbox mode defines access.
        if resume_session_id and schema_path:
            # Fail closed like an empty Claude schema. The caller must choose resume without a
            # schema and validate afterward, or start a fresh session with a schema.
            raise ValueError("codex: schema and resume are incompatible (codex#14343); choose one")
        cmd = [self.binary, "exec"]
        if resume_session_id:
            cmd += ["resume", resume_session_id]  # Subcommand, not a --resume flag.
            # `exec resume` rejects --sandbox, so pass the mode as a configuration override.
            sandbox = ["-c", "sandbox_mode=workspace-write"]
        else:
            sandbox = ["--sandbox", "workspace-write"]
        # Autonomous headless run: never request approval and allow workspace writes.
        cmd += ["--json", "-c", "approval_policy=never", *sandbox]
        # A tool working directory may not be a Git repository; without this flag Codex refuses it.
        # "Not inside a trusted directory".
        cmd += ["--skip-git-repo-check"]
        # A Claude-family name produces 400 invalid_request on a ChatGPT subscription. Omit --model
        # to use the account default; pass explicit Codex model names through unchanged.
        if model and not _claude_family(model):
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]  # Configuration override, not --effort.
        if system_prompt_path:
            cmd += ["-c", f"model_instructions_file={system_prompt_path}"]
        if schema_path:
            schema = Path(schema_path).read_text("utf-8").strip()
            if not schema:
                raise ValueError(f"empty schema {schema_path}: refusing an unconstrained call")
            cmd += ["--output-schema", schema_path]  # Codex expects a path; Claude expects inline JSON.
        cmd.append(prompt)
        return cmd

    def parse_output(self, stdout, exit_code) -> RunResult:
        """Parse a JSONL stream rather than Claude's single JSON object.

        Join text from content blocks in agent_message instead of using a list representation, map
        thread_id to session_id, and retain usage in raw. Skip malformed rows. If no valid JSON row
        exists, return an explicit failed result with the raw output.
        """
        text_parts: list[str] = []
        usage = err = session_id = None
        parsed_any = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(ev, dict):
                continue
            parsed_any = True
            t = ev.get("type")
            if t == "thread.started":
                session_id = ev.get("thread_id")  # Codex equivalent of session_id for resume.
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
            itype = item.get("item_type") or item.get("type")
            if t == "item.completed" and itype == "agent_message":
                txt = item.get("text")
                if txt is None:
                    txt = item.get("content")
                if isinstance(txt, list):
                    txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
                if txt:
                    text_parts.append(str(txt))
            elif t == "turn.completed":
                usage = ev.get("usage")
            elif t == "error" or ev.get("is_error"):
                err = str(ev.get("message") or ev.get("error") or "codex error")
        if not parsed_any:
            # Preserve non-JSONL output from a crash before formatting; classify will inspect stderr.
            return RunResult(ok=False, text=stdout or "", exit_code=exit_code)
        ok = exit_code == 0 and err is None
        # Store err in raw["error"]. base.classify includes it in the searchable blob, exposing
        # quota text from an error event without duplicating classification logic in adapters.
        return RunResult(ok=ok, text="\n".join(text_parts), exit_code=exit_code,
                         session_id=session_id, cost_usd=None,
                         raw={"usage": usage, "error": err or ""})
