"""Check evidence against source text: is the quote verbatim and does its locator point there?

The schema guarantees that a claim has quote and source_id fields. It cannot guarantee that the
quote occurs in its source because this is a content check rather than a shape check. Every
consumer previously reimplemented it. A production run over six courses recorded 62 failures in
this area, and each consumer wrote another one-off Python script.

This module detects all three discrepancy classes observed in production:

1. An ASR paragraph break. A visually continuous sentence crosses an empty line or physical line
   break. When copied into JSON, the double newline collapses into a space, so quote is no longer a
   byte-for-byte substring. Whitespace normalization fixes this by comparing joined text.
2. A shifted locator. PDF extractors systematically pointed to a neighboring marker line such as
   `<!-- Start of picture text -->` while the text began on the next line. This is reported as
   `locator-mismatch`, with the lines that actually contain the quote, rather than as not found.
3. A fabricated quote. The source contains no form of the quote. `quote-not-found` is the only one
   of these three discrepancy classes that must fail a batch.

Retraction removes a finding from the gate without hiding it. A claim with `status: retracted` is a
tombstone retained as history under the append-only final rule. After legitimate corpus cleanup,
such as repointing a source to a corrected transcript, its old quote may no longer exist. Treating
that as an error would make the gate permanently red and hide new failures in resolved history.
Findings for retracted claims therefore remain visible at info level without changing the exit code.

This module is read-only: it never changes source files or locators. The consumer decides what to
repair using the `suggested_locator` field.
"""
import re
import unicodedata
from pathlib import Path

from ...store import _claims_path, _scan_jsonl, _sources_path
from ...topic import find_topic

_WS = re.compile(r"\s+")
# Converter markup noise can break verbatim matching without changing a line's meaning. Remove HTML
# breaks from PDF/HTML extractors and their structural block markers.
_MARKUP = re.compile(r"<br\s*/?>|<!--.*?-->", re.IGNORECASE | re.DOTALL)


def _norm(text: str) -> str:
    """Normalize comparison text to NFC, remove markup noise, and collapse whitespace.

    NFC is required because macOS may return decomposed characters in NFD while model JSON arrives
    in NFC. Their bytes differ even when the rendered text is identical.
    """
    text = unicodedata.normalize("NFC", text)
    return _WS.sub(" ", _MARKUP.sub(" ", text)).strip()


def _source_lines(path: Path) -> list[str]:
    """Split file lines exactly as `nl -ba` numbers them, using only newline as a separator.

    str.splitlines() also splits on U+0085/U+2028/U+2029, which can occur inside text extracted
    from PDFs. That shifts numbering relative to ordinary command-line tools used by an agent, as
    observed in a production course corpus.
    """
    text = path.read_text("utf-8")
    lines = text.split("\n")
    if lines[-1:] == [""]:
        lines.pop()
    return lines


def _parse_line_locator(value: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", value)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (a, b) if a <= b else (b, a)


def _flatten(lines: list[str]) -> tuple[str, list[tuple[int, int, int]]]:
    """Represent a file as one normalized string plus a map from offsets to line numbers.

    A sliding line window fails for ASR transcripts whose paragraphs can reach 1,300 characters.
    Any heuristic for extending the window either truncates long quotes or degenerates into a full
    search. Flat text gives an exact single-pass search, while the map converts matched offsets back
    to line numbers.
    """
    parts: list[str] = []
    segments: list[tuple[int, int, int]] = []
    pos = 0
    for idx, line in enumerate(lines, 1):
        seg = _norm(line)
        if not seg:
            continue  # A marker or empty line contains no text after normalization.
        if parts:
            parts.append(" ")
            pos += 1
        parts.append(seg)
        segments.append((pos, pos + len(seg), idx))
        pos += len(seg)
    return "".join(parts), segments


def _find_spans(lines: list[str], needle: str) -> list[tuple[int, int]]:
    """Return inclusive, one-based line ranges whose joined text contains the quote.

    Every occurrence yields exactly one tight range covered by the match. Adjacent paragraphs do
    not inflate ranges, so `suggested_locator` can be substituted directly.
    """
    text, segments = _flatten(lines)
    spans = []
    start = text.find(needle)
    while start != -1:
        end = start + len(needle)
        touched = [ln for a, b, ln in segments if b > start and a < end]
        if touched:
            spans.append((min(touched), max(touched)))
        start = text.find(needle, start + 1)
    return spans


def verify_quotes(topic_dir: str | Path, *, zone: str = "all") -> list[dict]:
    """Check every evidence record in each claim of a zone against its source text.

    Return a list of findings; an empty list means all quotes are verbatim and all locators match.
    Codes are quote-not-found (error), locator-mismatch (warn), source-unreadable (error),
    locator-missing (warn), and locator-unparsed (warn). Every finding on a retracted claim is
    lowered to info and includes status: history stays visible without failing the live-knowledge gate.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    sources = {}
    for row, in ((r,) for r in _scan_jsonl(_sources_path(topic_dir))[0]):
        sources[row["id"]] = row
    cache: dict[str, list[str] | None] = {}

    def lines_of(src_id: str) -> list[str] | None:
        if src_id in cache:
            return cache[src_id]
        src = sources.get(src_id)
        cp = (src or {}).get("content_path")
        if not cp:
            cache[src_id] = None
            return None
        path = Path(cp)
        if not path.is_absolute():
            path = topic_dir / cp
        try:
            cache[src_id] = _source_lines(path)
        except OSError:
            cache[src_id] = None
        return cache[src_id]

    zones = ("staging", "final") if zone == "all" else (zone,)
    findings: list[dict] = []
    for z in zones:
        for claim in _scan_jsonl(_claims_path(topic_dir, z))[0]:
            first = len(findings)
            for k, ev in enumerate(claim.get("evidence", [])):
                src_id = ev.get("source_id", "")
                where = {"zone": z, "claim": claim.get("id"), "evidence": k,
                         "source_id": src_id}
                lines = lines_of(src_id)
                if lines is None:
                    findings.append({**where, "level": "error", "code": "source-unreadable",
                                     "detail": "the source has no readable content_path, so the "
                                               "quote cannot be checked"})
                    continue
                needle = _norm(ev.get("quote", ""))
                if not needle:
                    findings.append({**where, "level": "error", "code": "quote-not-found",
                                     "detail": "empty quote"})
                    continue
                spans = _find_spans(lines, needle)
                if not spans:
                    findings.append({**where, "level": "error", "code": "quote-not-found",
                                     "quote": ev.get("quote", "")[:160],
                                     "detail": "the source lacks the quote even after whitespace "
                                               "normalization; this is fabrication, not an ASR break"})
                    continue
                loc = ev.get("locator")
                if not loc:
                    findings.append({**where, "level": "warn", "code": "locator-missing",
                                     "suggested_locator": f"{spans[0][0]}-{spans[0][1]}"})
                    continue
                if loc.get("type") != "line":
                    continue  # Timestamp, page, and heading locators have no comparable line range.
                got = _parse_line_locator(str(loc.get("value", "")))
                if got is None:
                    findings.append({**where, "level": "warn", "code": "locator-unparsed",
                                     "locator": loc.get("value"),
                                     "suggested_locator": f"{spans[0][0]}-{spans[0][1]}"})
                    continue
                # Treat overlapping ranges as a match. A quote on lines 44-45 and locator 45-45
                # refer to the same text, exactly the PDF marker-line case.
                if not any(got[0] <= b and a <= got[1] for a, b in spans):
                    findings.append({**where, "level": "warn", "code": "locator-mismatch",
                                     "locator": loc.get("value"),
                                     "quote": ev.get("quote", "")[:160],
                                     "suggested_locator": f"{spans[0][0]}-{spans[0][1]}",
                                     "detail": "the quote is verbatim but occurs on different lines"})
            # Lower the level instead of skipping the claim. The tombstone remains visible as
            # history without failing the live-knowledge gate, as the module docstring explains.
            if claim.get("status") == "retracted":
                findings[first:] = [{**f, "level": "info", "status": "retracted"}
                                    for f in findings[first:]]
    return findings
