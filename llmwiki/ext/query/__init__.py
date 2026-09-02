"""Read topics through lists, queries, funnel statistics, and completeness audits without mutation.

Although files are directly available on disk, the socket is often consumed by an LLM in a
headless session with neither memory between calls nor room for jq mistakes. A production run over
six courses recorded 26 separate failures in ad hoc jq filters, including context shifts after
slurp, applying `.[]` to a JSONL string, and truncated output from a full claims dump. It also
recorded 74 cases of bypassing the socket in general. Reading deserves the same contract as writing:
one command, predictable JSON, and selectable fields.

This module is read-only. Every mutation belongs in store.py.

A retraction is not knowledge. A claim with `status: retracted` remains in its file as history under
the append-only final rule, but is never counted as live knowledge in this module, in either the
`stats` funnel or `audit` findings. Otherwise reports describe knowledge that no longer exists. In a
2026-08-26 production case, 289 claims were retracted after an ASR loop produced false material.
Nine dead staging rows then kept a permanent "staging is not processed" warning that could not be
resolved, while four sources appeared used despite supporting only tombstones. Compatibility with
old report totals was deliberately not prioritized. History remains visible through the separate
`claims_retracted` field and raw `list-claims` rows, where status is included by default. A page that
depends on a retracted fact is reported as `page-claims-retracted`.
"""
import json
import re
from pathlib import Path

from ...frontmatter import FrontmatterError, parse_page
from ...store import _claims_path, _scan_jsonl, _sources_path, _wiki_dir
from ...topic import find_topic

ZONES = ("staging", "final")

# A claim anchor in a page body follows the synthesis convention of marking every covered thesis
# with its claim id. The audit checks that marker rather than only the frontmatter list: a claim
# named in the header but absent from the body is incomplete synthesis.
_ANCHOR = re.compile(r"clm_[0-9a-f]{12}")


def _rows(path: Path) -> list[dict]:
    """Read for inspection, skipping bad rows so one row cannot prevent a report.

    Integrity problems are the responsibility of validate.
    """
    rows, _bad = _scan_jsonl(path)
    return rows


def _words(text: str) -> int:
    return len(text.split())


def read_claims(topic_dir: str | Path, zone: str = "all") -> list[dict]:
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    zones = ZONES if zone == "all" else (zone,)
    for z in zones:
        if z not in ZONES:
            raise ValueError(f"zone {z!r} is not one of {list(ZONES) + ['all']}")
    out = []
    for z in zones:
        for row in _rows(_claims_path(topic_dir, z)):
            out.append({**row, "zone": z})
    return out


def read_sources(topic_dir: str | Path) -> list[dict]:
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    return _rows(_sources_path(topic_dir))


def read_pages(topic_dir: str | Path) -> list[dict]:
    """Return synthesis pages with parsed frontmatter, body, and body size."""
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    wiki_dir = _wiki_dir(topic_dir)
    if not wiki_dir.is_dir():
        return []
    pages = []
    for md in sorted(wiki_dir.glob("*.md")):
        try:
            page, body = parse_page(md.read_text("utf-8"))
        except (FrontmatterError, UnicodeDecodeError, OSError) as e:
            pages.append({"path": str(md), "slug": md.stem, "error": str(e)})
            continue
        pages.append({**page, "path": str(md), "slug": md.stem,
                      "words": _words(body), "anchors": sorted(set(_ANCHOR.findall(body)))})
    return pages


def list_claims(topic_dir: str | Path, *, zone: str = "all", tag: str | None = None,
                source: str | None = None, contains: str | None = None,
                fields: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Query claims with filters; fields narrows output to prevent truncation.

    Default fields are deliberately compact: id, zone, status, and text. A complete claims layer
    with evidence exceeds harness tool output once a topic contains roughly fifty claims.
    """
    rows = read_claims(topic_dir, zone)
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    if source:
        rows = [r for r in rows if any(e.get("source_id") == source
                                       for e in r.get("evidence", []))]
    if contains:
        needle = contains.lower()
        rows = [r for r in rows if needle in r.get("text", "").lower()]
    if limit is not None:
        rows = rows[:limit]
    fields = fields or ["id", "zone", "status", "text"]
    if fields == ["*"]:
        return rows
    return [{f: r.get(f) for f in fields} for r in rows]


def source_words(topic_dir: str | Path) -> dict:
    """Count source words through content_path, returning None for a missing file.

    Returning zero would misrepresent the compression ratio.
    """
    topic_dir = Path(topic_dir)
    out = {}
    for src in read_sources(topic_dir):
        cp = src.get("content_path")
        if not cp:
            out[src["id"]] = None
            continue
        path = Path(cp)
        if not path.is_absolute():
            path = topic_dir / cp
        try:
            out[src["id"]] = _words(path.read_text("utf-8"))
        except OSError:
            out[src["id"]] = None
    return out


def stats(topic_dir: str | Path) -> dict:
    """Return the topic funnel as one object: sources, claims, pages, volume, and density.

    Wiki words per claim is the primary synthesis-quality metric observed in production runs.
    Below 60 words usually produces a skeleton; above 250 tends to produce padding. Computing it
    here keeps consumers from reimplementing the calculation with divergent definitions.

    Every knowledge counter measures live rows. `claims_staging` and `claims_final` exclude
    `status: retracted`, and their derived fields do the same: `words_per_claim`,
    `claims_per_source`, `final_claims_on_pages`, and `final_claims_orphan`. A dead row contributes
    no knowledge, so dividing wiki volume by it understates density and misrepresents the funnel.
    Tombstones remain visible through `claims_retracted`, which counts both zones. Before
    2026-08-26 it counted only final and made staging retractions entirely invisible.
    """
    topic_dir = Path(topic_dir)
    manifest = find_topic(topic_dir)
    sources = read_sources(topic_dir)
    sw = source_words(topic_dir)
    staging = read_claims(topic_dir, "staging")
    final = read_claims(topic_dir, "final")
    live_staging = [c for c in staging if c.get("status") != "retracted"]
    live_final = [c for c in final if c.get("status") != "retracted"]
    pages = read_pages(topic_dir)
    wiki_words = sum(p.get("words", 0) for p in pages)
    assigned = {cid for p in pages for cid in p.get("claims", [])}
    final_ids = {c["id"] for c in live_final}
    src_words_known = [v for v in sw.values() if v is not None]
    per_claim = round(wiki_words / len(live_final), 1) if live_final else None
    return {
        "slug": manifest["slug"],
        "sources": len(sources),
        "sources_words": sum(src_words_known),
        "sources_without_content": sum(1 for v in sw.values() if v is None),
        "claims_staging": len(live_staging),
        "claims_final": len(live_final),
        "claims_retracted": (len(staging) - len(live_staging)) + (len(final) - len(live_final)),
        "pages": len(pages),
        "wiki_words": wiki_words,
        "words_per_claim": per_claim,
        "wiki_share_percent": (round(wiki_words * 100 / sum(src_words_known), 2)
                               if src_words_known and sum(src_words_known) else None),
        "claims_per_source": (round(len(live_final) / len(sources), 1) if sources else None),
        "final_claims_on_pages": len(assigned & final_ids),
        "final_claims_orphan": len(final_ids - assigned),
    }


def audit(topic_dir: str | Path, *, min_words_per_claim: float = 60.0,
          max_words_per_claim: float = 250.0, require_anchors: bool = True) -> list[dict]:
    """Audit synthesis completeness that validate intentionally does not cover.

    validate answers whether the format is intact. This function answers whether the work is
    complete: every final claim is assigned to pages, pages do not restate one another, volume is
    proportional to claims, and staging has been processed. It was added after a production topic
    passed `validate` despite missing five PDF sources, allowing the queue to continue as though the
    corpus were complete.

    Retracted claims are not knowledge: they do not await staging review, do not make a source used,
    and are not covered by a page. A page that references them instead produces the error-level
    `page-claims-retracted` finding because a withdrawn fact remains in finished knowledge. Page
    density still uses the frontmatter list as written. While it includes a tombstone, the link
    itself must be fixed rather than explaining the result through word counts.

    Return findings as {level, code, ...}. error means the work is incomplete, warn asks the
    consumer to decide, and an empty list is clean.
    """
    topic_dir = Path(topic_dir)
    findings: list[dict] = []
    final = read_claims(topic_dir, "final")
    staging = read_claims(topic_dir, "staging")
    pages = read_pages(topic_dir)
    live_final = [c for c in final if c.get("status") != "retracted"]
    final_ids = {c["id"] for c in live_final}
    retracted_ids = {c["id"] for c in final if c.get("status") == "retracted"}

    broken = [p for p in pages if "error" in p]
    for p in broken:
        findings.append({"level": "error", "code": "page-unreadable",
                         "page": p["slug"], "detail": p["error"]})
    pages = [p for p in pages if "error" not in p]

    seen: dict[str, list[str]] = {}
    for p in pages:
        for cid in p.get("claims", []):
            seen.setdefault(cid, []).append(p["slug"])

    orphan = sorted(final_ids - set(seen))
    if orphan:
        findings.append({"level": "error", "code": "claims-orphan", "count": len(orphan),
                         "claims": orphan[:20],
                         "detail": "final claims do not appear on any page"})
    multi = {cid: pgs for cid, pgs in seen.items()
             if len(pgs) > 1 and cid not in retracted_ids}
    if multi:
        findings.append({"level": "warn", "code": "claims-on-many-pages", "count": len(multi),
                         "claims": dict(list(multi.items())[:20]),
                         "detail": "a claim is assigned to multiple pages - "
                                   "check that this is not duplicate restatement"})
    # A retracted staging row is resolved history, not unfinished work. It can neither be promoted
    # nor revived, so warning about it would be permanent. One production topic reached that dead
    # end with all nine staging rows being tombstones.
    unsorted_staging = [c for c in staging if c.get("status") != "retracted"]
    if unsorted_staging:
        findings.append({"level": "warn", "code": "staging-not-empty",
                         "count": len(unsorted_staging),
                         "detail": "verified claims remain in staging and are absent from both "
                                   "final and pages"})
    for p in pages:
        claims = p.get("claims", [])
        if not claims:
            findings.append({"level": "error", "code": "page-without-claims",
                             "page": p["slug"]})
            continue
        dead = sorted(set(claims) & retracted_ids)
        if dead:
            findings.append({"level": "error", "code": "page-claims-retracted",
                             "page": p["slug"], "count": len(dead), "claims": dead[:20],
                             "detail": "the page depends on retracted claims, leaving a withdrawn "
                                       "fact in finished knowledge; rebuild the page"})
        density = p["words"] / len(claims)
        if density < min_words_per_claim:
            findings.append({"level": "error", "code": "page-too-thin", "page": p["slug"],
                             "words": p["words"], "claims": len(claims),
                             "words_per_claim": round(density, 1),
                             "detail": f"below {min_words_per_claim:g} words per claim - likely a skeleton"})
        elif density > max_words_per_claim:
            findings.append({"level": "warn", "code": "page-too-fat", "page": p["slug"],
                             "words": p["words"], "claims": len(claims),
                             "words_per_claim": round(density, 1),
                             "detail": f"above {max_words_per_claim:g} words per claim - likely padded"})
        if require_anchors:
            # Exclude retracted claims because page-claims-retracted already explains them; a second
            # finding for the same claim would add only noise.
            unanchored = sorted(set(claims) - set(p.get("anchors", [])) - retracted_ids)
            if unanchored:
                findings.append({"level": "warn", "code": "claims-without-anchor",
                                 "page": p["slug"], "count": len(unanchored),
                                 "claims": unanchored[:20],
                                 "detail": "the claim appears in frontmatter but is not marked "
                                           "in the page body"})
    # Evidence from a dead claim does not make its source useful. After retraction the source is
    # silent again, which must remain visible; otherwise corpus cleanup hides sources supporting
    # nothing else.
    used_sources = {e.get("source_id") for c in live_final for e in c.get("evidence", [])}
    silent = [s["id"] for s in read_sources(topic_dir) if s["id"] not in used_sources]
    if silent:
        findings.append({"level": "warn", "code": "sources-without-claims", "count": len(silent),
                         "sources": silent[:20],
                         "detail": "the source is registered but produced no live final claims; "
                                   "retracted claims do not count"})
    return findings


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
