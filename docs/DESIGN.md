# Design

## Role and boundaries

`llmwiki` is an executable knowledge contract. Mutators write through its Python API
or CLI, while readers may consume the self-contained files directly. The library owns
format validation and storage mutations. It does not own research planning, retrieval,
scheduling, model calls, or domain policy.

A topic is always an external directory supplied by the caller. The repository contains
no data home and the CLI deliberately has no `--home` option. This stronger boundary
prevents repository state, current working directory, and user data from becoming
implicitly coupled.

## Topic format - specification version 0.1

```text
<slug>/
  topic.json                    topic manifest
  sources/sources.jsonl        topic-level source registry
  sources/content/             optional content referenced by every zone
  work/                        mutator-owned plans, drafts, and checkpoints
  staging/claims.jsonl         candidate, verified, or retracted claims
  final/claims.jsonl           verified, final, or retracted claims
  final/wiki/<concept>.md      synthesis with YAML frontmatter
```

The JSON schemas in `llmwiki/spec/` are the single source of format rules. New format
rules belong in a schema rather than parallel validation code. The small schema engine
fails closed: any validation keyword it does not understand raises `SpecError`. A rule
cannot silently exist in the specification without enforcement.

Breaking format changes require a specification-version bump and a migration in the
same release. This release retains version 0.1 and does not alter schema shape.

## Identity and references

- `src_`, `clm_`, and `wik_` identifiers use SHA-256-derived deterministic hashes.
- A source uses its URL as primary identity. Without a URL it uses kind plus title.
- A claim id derives from normalized text within a topic, providing exact deduplication.
- A wiki id derives from normalized topic slug plus concept title.
- Every evidence `source_id` must exist in `sources.jsonl`, including in staging.
- Every wiki claim id must exist in `final/claims.jsonl`.

Paths are not identifiers because renaming a file must not change entity identity.
Semantic deduplication remains consumer policy; this layer guarantees only exact
normalized identity.

## Zone and mutation rules

Staging accepts candidate, verified, and retracted status. Final accepts verified,
final, and retracted status. These rules are executable gates rather than conventions.

Claims are append-only except for two explicit changes to an existing row:

1. `retract_claim` changes only status to retracted and retains the tombstone row.
2. `merge_claim_evidence` only grows the evidence list. It never removes or changes an
   existing evidence record and rejects growth on a tombstone.

Promotion moves a verified row from staging to final without changing status. It writes
final first and then rewrites staging. An interruption between the operations can leave
a duplicate across zones, which a repeated promotion reconciles; the order prevents data
loss. Batch promotion locks staging before final for every caller to avoid deadlock and
rejects the complete batch before writing if any input is missing or not verified.

`set_claim_status` currently permits only candidate to verified in staging. It never
revives a retracted claim. The CLI's quote-verification callback runs after reading the
batch and before changing rows under the same staging lock, closing the race in which
evidence could change between verification and status update.

Wiki pages are the mutable synthesis layer. `add`, `update`, `put`, and `delete` share
one wiki lock because a filename is derived from a title and concurrent writers for the
same concept could otherwise choose different files. Explicit slugs are single, dot-free
path components. Resolved-path checks prevent deletion through a symlink outside
`final/wiki`.

## Concurrency and atomicity

Every JSONL mutation is read-modify-write under an adjacent hidden `flock` file. The lock
uses a separate inode because `os.replace` replaces the data file inode. Lock files remain
on disk to prevent a delete-and-reopen race.

Temporary filenames include process id and a random UUID. A shared `<path>.tmp` caused a
production lost-write incident when one process replaced another process's temporary
file. Writes flush and atomically replace the target, so readers see either the old or
new complete file, never a torn row.

`LLMWIKI_LOCK_TIMEOUT` defaults to 60 seconds. Waiting tolerates normal sub-second
contention from neighboring mutators, while the finite timeout detects a stalled holder.

## Corruption handling

Write paths fail closed on any unreadable or malformed JSONL row. `CorruptLine` carries
all findings in a file because silently skipping one row would remove facts from both
foreign-key and deduplication checks.

Validation retains valid rows while reporting every damaged row. This prevents one real
parse error from causing a cascade of false missing-reference findings. Empty or
whitespace-only rows inside JSONL are corruption; a zero-byte file is a valid empty zone.
Only LF separates records, with CR stripped for CRLF. Unicode U+0085, U+2028, and U+2029
inside strings are not treated as record separators.

The custom YAML subset supports string scalars and block-style lists of strings. It does
not infer numbers or booleans. Ambiguous strings are JSON-quoted for round-trip safety,
and unparseable frontmatter raises `FrontmatterError` rather than guessing.

## Read model and quality gates

`validate_topic` checks format integrity: manifest and schemas, zone status, foreign keys,
duplicate ids, JSONL corruption, and wiki frontmatter. It deliberately does not claim the
research or synthesis is complete.

`ext.query.audit` checks completeness: orphan final claims, page density, unprocessed
verified staging, claim anchors, silent sources, claims assigned to multiple pages, and
pages that depend on retracted claims. Retracted rows do not count as live knowledge in
statistics or audit coverage, but remain visible through raw listing and the separate
`claims_retracted` count.

`ext.evidence.verify_quotes` checks each quote against source content after NFC,
converter-markup, and whitespace normalization. A missing quote or unreadable source is
an error. Missing, unparsable, or shifted line locators are warnings. Findings on a
retracted claim remain visible as info and do not fail the live-knowledge gate.

## CLI contract

The CLI returns 0 for success and 1 for all validation, duplicate, foreign-key,
configuration, and usage failures. It does not return quota or retry codes because the
knowledge socket is offline. A custom `ArgumentParser` maps usage errors to 1 instead of
stock argparse's 2. Successful machine-readable commands emit JSON or ids on stdout;
diagnostics go to stderr with a violated rule.

`doctor` discovers every schema file on disk instead of using a hard-coded name list. It
therefore fails if a newly added schema uses an unsupported keyword or if a core schema
is missing. With an optional topic path, it also reads and validates the manifest without
mutating the topic.

## Extension boundary

`llmwiki/ext/` contains optional read and verification capabilities. Extensions may
import the core; the core does not import extensions except where the CLI explicitly
registers a command. `llmwiki/shared/` is vendored scaffolding owned by this repository.
The socket itself uses only `shared.cliargs`; agent adapters remain available for future
extensions but are not invoked by knowledge operations.

The package has no runtime dependency outside Python's standard library. It performs no
network or LLM call and reads no credentials.
