# AGENTS.md - tool-llm-wiki

## Purpose and boundaries

This repository contains `llmwiki`, a local schema-driven knowledge format and
mutation API. It validates and updates sources, claims, evidence, and synthesized
wiki pages through packaged schemas, deterministic identifiers, foreign-key checks,
interprocess locks, and atomic writes. It is a validator, not an agent.

All topic data lives outside the checkout in a directory supplied explicitly by the
caller. The tool has no data-home default and no home option. It performs no network
or model calls, reads no credentials, and has no runtime dependency outside the
Python standard library. LLMWIKI_LOCK_TIMEOUT controls only lock waiting and never
selects a data path. Research policy, retrieval, scheduling, and semantic
deduplication belong to consumers.

The distribution and import package are both named `llmwiki`. Consumers install and
import that name, so renaming it is a public contract break.

## Reading order

1. Read `README.md` for the format, public API, quick start, and limits.
2. Read `docs/DESIGN.md` for invariants, zones, concurrency, and failures.
3. Read `llmwiki/spec/` as the single source of format rules.
4. Read `tests/` as the executable specification.
5. Read `tests/conftest.py` before changing test isolation.

## Verification

Run the complete hermetic suite from the repository root before reporting success:

```bash
python3 -m pytest -q -p no:cacheprovider
```

Report the observed result. Packaging behavior is covered separately by
`smoke/smoke_contract.py`; use it when changing package discovery or package data.

## Working rules

- Tests must not contact the network, invoke an agent or model, read the user's home
  directory, or depend on the caller's working directory. Keep all artifacts in
  temporary directories.
- Never commit topic data, corpora, model files, logs, credentials, private
  hostnames, signed URLs, or developer-machine absolute paths.
- A new format rule belongs in a schema under `llmwiki/spec/`. Validation logic must
  read the schema rather than create a parallel source of truth.
- Validation fails closed. Unknown schema keywords and malformed or almost-valid
  input are errors, not hints to ignore or repair silently.
- Supported writes go through the mutation API. Claims remain append-only except for
  tombstone retraction and evidence-list growth.
- Every JSONL mutation holds its adjacent lock for the read-modify-write cycle and
  uses a unique temporary file followed by atomic replacement.
- The core imports no neighboring repository. Consumers may import this package; it
  calls no other tool.
- Extensions live under `llmwiki/ext/` and may import the core. The core reaches an
  extension only through explicit CLI registration.
- `llmwiki/shared/` is vendored scaffolding owned by this repository. Treat an edit
  there as a deliberate fork and explain why in the commit message.
- Keep changes minimal and preserve strictness unless the contract explicitly
  changes.

## Contract changes

The specification version, schemas, topic layout, identifier derivation, zone and
status rules, mutation semantics, package name, and CLI outputs and exit codes are
public contracts. A breaking format change requires a version bump and a migration
in the same change. Update `README.md`, `docs/DESIGN.md`, and tests together. Changes
to package discovery or schema packaging also require the installed-package smoke.

## Style

Use English for code, comments, documentation, diagnostics, and commit messages,
except fixtures whose explicit purpose is Unicode behavior. Use no emoji or dash
characters in place of a plain hyphen. Keep commit subjects short and imperative;
use the body to explain why. Add complexity only for a current need.
