# ext/query - read a topic

This is the socket's read-only layer for lists, filtered queries, funnel statistics,
and synthesis completeness audits. Every mutation remains in `llmwiki/store.py`.

The layer was added after a production run over six courses. Before it existed,
consumers read `claims.jsonl` directly and produced 26 failures in ad hoc jq filters,
plus frequent output truncation when dumping a complete claims layer.

## Commands

- `list-claims <topic_dir> [--zone staging|final|all] [--tag T] [--source src_...]
  [--contains text] [--fields id,text|*] [--limit N]` filters claims. Compact
  default fields prevent harness output truncation when evidence is large.
- `list-sources <topic_dir>` returns the source registry.
- `list-wiki <topic_dir>` returns pages with slug, path, claim count, body size,
  and `clm_...` anchors found in the body.
- `stats <topic_dir>` returns one funnel object covering source words, live staging
  and final claims, retracted claims, pages, wiki words, words per claim, and the
  source-to-wiki compression ratio.
- `audit <topic_dir> [--min-words-per-claim N] [--max-words-per-claim N]
  [--no-anchors]` checks work completeness rather than file format and exits 1 for
  error-level findings.

`validate` answers whether the format is intact. `audit` answers whether the work is
complete. A production incident demonstrated the distinction: a topic passed
`validate` after its corpus omitted five PDF sources, and the queue continued as if
the topic were complete.

## Audit findings

| code | level | meaning |
|---|---|---|
| `claims-orphan` | error | A final claim appears on no page. |
| `page-too-thin` | error | Words per claim are below the threshold, producing a skeleton. |
| `page-without-claims` | error | Page frontmatter contains no claims. |
| `page-unreadable` | error | Page frontmatter cannot be parsed. |
| `page-claims-retracted` | error | A page depends on a retracted claim. |
| `claims-on-many-pages` | warn | A claim is assigned to multiple pages. |
| `claims-without-anchor` | warn | A frontmatter claim is not marked in the body. |
| `page-too-fat` | warn | Words per claim exceed the threshold and may indicate padding. |
| `staging-not-empty` | warn | Verified claims remain unprocessed in staging. |
| `sources-without-claims` | warn | A source produced no live final claim. |

## A retraction is not knowledge

A claim with `status: retracted` remains as a tombstone row for history, but neither
`stats` nor `audit` counts it as live knowledge. It does not wait for staging review,
make a source used, receive page coverage, or contribute to claims and derived ratios.

This rule followed cleanup of an ASR loop on 2026-08-26. After 289 claims were
retracted, nine dead staging rows kept a permanent warning that could not be resolved,
while four sources appeared used despite supporting only tombstones. The selected
priority was for current metrics to describe current knowledge, rather than retain
numeric compatibility with old reports.

History remains available: `claims_retracted` counts tombstones in both zones, raw
`list-claims` rows include status, and a finished page referencing a dead claim raises
`page-claims-retracted`. The report mentions such a claim exactly once by excluding it
from anchor and multiple-page findings. Page density still uses the frontmatter list as
written because the dead reference itself must be repaired before interpreting ratios.
The same rule applies in `ext/evidence`, where quote findings on retracted claims remain
visible as `info` without failing the gate.
