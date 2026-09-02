# ext/evidence - verify quotes against sources

`verify-quotes <topic_dir> [--zone staging|final|all]` compares every
`evidence.quote` with the text referenced by its source's `content_path`. It is
read-only: source files and locators are never changed, and a mismatch includes a
`suggested_locator` when possible.

This belongs in the socket because a schema can require a quote but cannot prove
that it occurs verbatim in source content. Before this extension, every consumer
implemented that check independently. A production run over six courses recorded
62 failures in this area and repeatedly created one-off Python scripts.

## Finding codes

| code | level | meaning |
|---|---|---|
| `quote-not-found` | error | The normalized source does not contain the quote. |
| `source-unreadable` | error | The source has no readable `content_path`. |
| `locator-mismatch` | warn | The quote is verbatim but occurs on different lines. |
| `locator-missing` | warn | No locator was supplied; a suggestion is included. |
| `locator-unparsed` | warn | `locator.value` is not an `N` or `N-M` line range. |

Finding level also depends on claim status. Every finding on a retracted claim is
returned as `info`, with `"status": "retracted"`, and does not cause exit 1. A
retraction is a historical tombstone. After legitimate cleanup, such as pointing a
source at a corrected transcript, its old quote may disappear. Treating that as an
error would make the gate permanently red and hide new failures among resolved ones.
The finding remains visible because hiding it would silently rewrite history.

## What counts as verbatim

Comparison normalizes text to NFC, removes `<br>` and HTML comments, and collapses
whitespace. These deliberate allowances cover three observed production cases:

- An ASR paragraph break can turn two newlines into a space when copied into JSON.
  The meaning is identical even though the bytes differ.
- PDF and HTML converters can leave markup noise such as `<br>` inside a line.
- Line numbering splits only on newline, matching `nl -ba`. `str.splitlines()` also
  splits U+0085/U+2028/U+2029 found in extracted PDF text and shifts every locator
  relative to what ordinary command-line tools show.

A locator matches when its range intersects the quote's range rather than requiring
equality. A quote on lines 44-45 with locator `45-45` refers to the same text. This
matches an observed PDF extraction error where a locator selected a marker line and
the actual text started on the following line.
