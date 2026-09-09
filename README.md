# llm-wiki

[![Tests](https://github.com/abetor/llm-wiki/actions/workflows/tests.yml/badge.svg)](https://github.com/abetor/llm-wiki/actions/workflows/tests.yml)

`llm-wiki` (Python package `llmwiki`) is a local, schema-driven knowledge format and
validated mutation API. It keeps
sources, claims, evidence, and synthesized wiki pages consistent without a database,
network service, language model, or credentials.

[Quick start](#quick-start) | [Offline demo](#synthetic-demo) | [Architecture](docs/DESIGN.md) | [Tests](#tests) | [Contributing and agent guide](AGENTS.md) | [MIT license](LICENSE)

## Problem

Knowledge-producing pipelines often write JSONL and Markdown directly. Over time,
their schemas drift, foreign keys break, duplicate facts accumulate, malformed rows
silently disappear, and a successful format check is mistaken for complete work.
Concurrent writers add another failure mode: a shared temporary file can lose a row
even after the caller received a successful result.

`llmwiki` makes those invariants executable. Every mutation passes through one validated
mutation API that checks packaged schemas and references, deduplicates exact claims,
serializes writers, and replaces files atomically. Separate read-only checks verify
quotes and audit synthesis completeness.

## What it provides

- A portable topic directory containing a manifest, sources, staging and final claims,
  and wiki pages.
- Deterministic `src_`, `clm_`, and `wik_` identifiers for exact deduplication.
- Fail-closed validation against the schemas in `llmwiki/spec/`.
- Real claim-to-source and page-to-final-claim foreign keys.
- Atomic writes with interprocess file locks and unique temporary paths.
- Batch claim insertion, promotion, status changes, and evidence merging.
- Historical retraction through tombstones rather than destructive deletion.
- Read-only listing, statistics, completeness auditing, and verbatim quote checks.
- A Python API and a CLI with stable exit codes: 0 for success and 1 for failure.

The distribution and import package are both named `llmwiki`.

## Architecture

```text
external topic directory
|
+-- topic.json                  manifest
+-- sources/
|   +-- sources.jsonl          source registry
|   +-- content/               optional source text
+-- work/                      consumer-owned drafts and checkpoints
+-- staging/claims.jsonl       candidate and verified claims
+-- final/
    +-- claims.jsonl           verified/final claims and tombstones
    +-- wiki/*.md              synthesized pages with YAML frontmatter

llmwiki/spec/*.schema.json     single source of format rules
llmwiki/store.py               validated and locked mutations
llmwiki/ext/query              read model, statistics, completeness audit
llmwiki/ext/evidence           quote and locator verification
llmwiki/cli.py                 command-line boundary
```

The core never imports a neighboring repository. Consumers can import the library in
process or invoke the CLI. They may read topic files directly, but supported writes go
through `llmwiki`.

See [docs/DESIGN.md](docs/DESIGN.md) for invariants and failure semantics.

## Quick start

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev]"
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider

topic_dir="$(python3 -m llmwiki init "$(mktemp -d)" "Agent memory" --slug agent-memory)"
printf '%s\n' '{"kind":"web","title":"Example","url":"https://example.org/article"}' | \
  python3 -m llmwiki add-source "$topic_dir"
python3 -m llmwiki validate "$topic_dir"
python3 -m llmwiki doctor "$topic_dir"
```

Every command that reads JSON accepts one object on stdin unless `--batch` is stated.
Errors are written to stderr as `duplicate: <id>` or `error: <violated rule>`.

## Synthetic demo

The following offline example creates readable source content, registers one source,
adds and promotes a verified claim, writes a page, verifies its quote, and audits the
finished topic. It writes only to a caller-selected temporary directory.

```bash
demo_root="$(mktemp -d)"
topic_dir="$(python3 -m llmwiki init "$demo_root" "Synthetic topic" --slug synthetic-topic)"
mkdir -p "$topic_dir/sources/content"
printf '%s\n' 'Atomic replacement prevents readers from seeing partial files.' \
  > "$topic_dir/sources/content/example.txt"

source_id="$(printf '%s\n' \
  '{"kind":"local","title":"Synthetic source","content_path":"sources/content/example.txt"}' | \
  python3 -m llmwiki add-source "$topic_dir")"

claim_id="$(printf '%s\n' \
  "{\"text\":\"Atomic writes hide partial files\",\"confidence\":\"high\",\"status\":\"verified\",\"evidence\":[{\"source_id\":\"$source_id\",\"quote\":\"Atomic replacement prevents readers from seeing partial files.\",\"stance\":\"supports\",\"locator\":{\"type\":\"line\",\"value\":\"1\"}}]}" | \
  python3 -m llmwiki add-claim "$topic_dir" staging)"

python3 -m llmwiki promote "$topic_dir" "$claim_id"
printf '%s\n' \
  "{\"title\":\"Atomic writes\",\"slug\":\"atomic-writes\",\"claims\":[\"$claim_id\"],\"body\":\"# Atomic writes\\n\\nThe writer uses atomic replacement ($claim_id).\"}" | \
  python3 -m llmwiki add-wiki "$topic_dir"
python3 -m llmwiki validate "$topic_dir"
python3 -m llmwiki verify-quotes "$topic_dir"
python3 -m llmwiki audit "$topic_dir" --min-words-per-claim 1
```

Expected final outputs are `OK`, `[]`, and `[]` for validation, quote verification,
and audit respectively.

Quote checks establish traceability to source text; they do not establish that a source or
claim is true. The example passes `status: verified` itself; deciding what deserves that
status belongs to the caller.

## Limitations

- Deduplication is exact after text normalization, not semantic.
- Promotion writes final before rewriting staging. A process interruption can leave a
  recoverable duplicate across zones, but does not lose the claim. Retrying promotion
  reconciles it.
- Locks use POSIX `flock` and assume one local filesystem. Network filesystems without
  reliable lock support are not supported.
- Complete JSONL files are rewritten on mutation. Behavior has not been benchmarked on
  corpora with tens of thousands of claims.
- Quote verification normalizes NFC, converter markup, and whitespace. It checks line
  locators but cannot validate timestamp, page, or heading locators from text alone.
- Agent CLI adapters under `llmwiki/shared/` are vendored extension scaffolding. The
  mutation API itself imports only the custom argument parser and does not invoke
  an agent, LLM, or network service.
- A few tests use Cyrillic text on purpose: readable slugs and JSON escaping for
  non-Latin titles are part of the contract.

## Data and credential boundary

The repository contains no topic data, corpora, model files, logs, credentials, or
credential-loading code. `llmwiki` has no data-home default and no `--home` option.
Every operation receives an explicit external `topic_dir`; `init` also receives an
explicit base directory. Data location is therefore always caller-configurable and is
never derived from the repository or current working directory.

The runtime uses only the Python standard library and performs no network or LLM calls.
`LLMWIKI_LOCK_TIMEOUT` changes only the maximum lock wait, defaulting to 60 seconds; it
does not select a data path. pytest is an optional development dependency.

## Tests

The complete hermetic suite redirects HOME and cwd into temporary directories and
removes declared credential variables before each test:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

The installed-package contract smoke uses temporary data and no network:

```bash
python3 -m pip install -e .
cd /tmp
PYTHONDONTWRITEBYTECODE=1 python3 /path/to/tool-llm-wiki/smoke/smoke_contract.py
```

## Provenance

This repository began as a public source snapshot of a personal tool. Earlier local development
history is not included.

## License

MIT. Copyright (c) 2026 abetor. See [LICENSE](LICENSE).
