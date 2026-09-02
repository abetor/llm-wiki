"""Knowledge write socket: the only mutation path. Validates, deduplicates, and writes atomically.

Zone rules become stricter toward final:
- staging: any status except final; the schema requires evidence.
- final: status must be verified or final; every evidence.source_id must exist
  in the source registry. This is a real foreign key, not a matching URL string.
  It fixes a defect in the prototype.
Foreign keys are also checked in staging: a claim pointing nowhere is meaningless in any zone.

Mutating a zone file is a read-modify-write operation, and every writer for the topic shares it.
Concurrent mutators, such as parallel agents and background batches, must therefore serialize on
an interprocess lock, and every process must use a unique temporary filename. Otherwise one
writer's os.replace can move the shared temporary file out from under another writer and silently
lose a record. A production incident on 2026-08-20 reproduced this with six parallel workers: one
claim received a valid id but never reached the file. See _locked and _tmp_path.
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .frontmatter import FrontmatterError, parse_page, render_page
from .ids import make_claim_id, make_source_id, make_wiki_id
from .topic import find_topic
from .validate import ValidationError, validate_object

_CONCEPT_SLUG = re.compile(r"\W+", re.UNICODE)

# Wait for the lock instead of failing immediately: a neighboring mutator writes for a fraction
# of a second, while the calling headless agent session cannot retry. The timeout prevents a
# stalled lock holder from causing an indefinite wait.
LOCK_TIMEOUT = float(os.environ.get("LLMWIKI_LOCK_TIMEOUT", "60"))

_ZONE_STATUS = {
    "staging": {"candidate", "verified", "retracted"},
    # Retracted is valid in final because it is a tombstone. Only retract_claim uses it to perform
    # the append-only mutation of an existing row described in DESIGN. The gate does not prohibit
    # add_claim from creating a retracted row, just as in staging, but that is not an intended path.
    "final": {"verified", "final", "retracted"},
}


class DuplicateClaim(Exception):
    """The same normalized text already exists in this zone; its id is derived from the text."""


class LockTimeout(Exception):
    """The zone lock was not acquired within LOCK_TIMEOUT; its holder stalled or died holding it."""


class CorruptLine(Exception):
    """A JSONL file cannot be read as rows of objects because it was edited outside the socket.

    This also covers a wholly unreadable file, including invalid UTF-8 and I/O errors. Parsing
    never reaches individual rows, but the effect is the same: content would disappear from
    foreign-key checks and deduplication.

    This has a separate type because it is not a content validation error: the schema is never
    reached. Writes reject such a file fail-closed. validate_topic must report the problem as a
    string in its result instead of raising a traceback because its contract is list[str].
    """


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _locked(path: Path):
    """Hold an exclusive interprocess lock for a zone file using adjacent <path>.lock.

    The lock belongs to a separate file, not path itself. os.replace gives path a new inode, so
    flock on the old inode would protect nothing after replacement. Nobody replaces the lock
    file, which makes it the serialization point. The lock file is retained because a race
    between deleting and reopening it would recreate the same defect.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A dot prefix keeps the lock file out of both *.md wiki traversal and the normal human view
    # of a zone.
    lock_path = path.parent / ("." + path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"{lock_path.name}: lock not acquired within {LOCK_TIMEOUT:g}s - "
                        "check for stalled llmwiki processes") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _tmp_path(path: Path) -> Path:
    """Return a temporary filename unique to both the process and the call.

    A shared `<path>.tmp` under concurrent writes loses data: a neighbor replaces its temporary
    file and ours disappears with FileNotFoundError, or worse, our replace overwrites the
    neighbor's complete record."""
    return path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")


def _scan_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    """Parse a whole file into valid rows and a list of damage reports (file:line: reason).

    One damaged row does not discard the rest. Otherwise valid sources and final claims would
    disappear from known/final_ids, causing a cascade of false foreign-key findings on top of the
    real problem, while later damage in the same file went unreported. An empty or whitespace-only
    row inside the file is also corruption: the file was edited outside the socket, and the next
    mutation would rewrite the whole file and silently erase that row. A zero-byte file is valid
    and means there are no rows, exactly as in a newly created zone. A wholly unreadable file,
    including invalid UTF-8, permission errors, or a directory in place of a file, is the same kind
    of finding. Parsing did not reach individual rows, but silence would remove the whole file from
    foreign-key checks."""
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return [], []
    except (UnicodeDecodeError, OSError) as e:
        return [], [f"{path.name}: file is unreadable ({type(e).__name__}: {e})"]
    rows: list[dict] = []
    bad: list[str] = []
    # JSONL records are separated only by LF; strip CR from CRLF. str.splitlines() is unsuitable
    # because it also splits on U+0085/U+2028/U+2029 inside a valid JSON string.
    lines = text.split("\n")
    if lines[-1:] == [""]:
        lines.pop()  # A final LF terminates the last row; it is not an empty row.
    for n, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip():
            bad.append(f"{path.name}:{n}: empty row - "
                       "every JSONL row must be an object")
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            bad.append(f"{path.name}:{n}: row is not valid JSON ({e})")
            continue
        if not isinstance(row, dict):
            bad.append(f"{path.name}:{n}: row is {type(row).__name__}, not an object")
            continue
        rows.append(row)
    return rows, bad


def _read_jsonl(path: Path) -> list[dict]:
    """Read before a write and reject any damage with all file findings in CorruptLine.

    Nothing is skipped silently. An unread row is a fact that would quietly disappear from
    foreign-key checks and deduplication, breaking the promise that invalid data never passes."""
    rows, bad = _scan_jsonl(path)
    if bad:
        raise CorruptLine("; ".join(bad))
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Rewrite atomically through os.replace; topic files are small and never contain torn rows."""
    tmp = _tmp_path(path)
    # Keep non-ASCII text readable, but escape JSON-valid Unicode line and paragraph separators so
    # external Unicode-aware readers cannot mistake them for JSONL record boundaries.
    payload = "".join(
        json.dumps(r, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        + "\n"
        for r in rows
    )
    tmp.write_text(payload, "utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, obj: dict) -> None:
    rows = _read_jsonl(path)
    rows.append(obj)
    _write_jsonl(path, rows)


def _sources_path(topic_dir: Path) -> Path:
    return topic_dir / "sources" / "sources.jsonl"


def _claims_path(topic_dir: Path, zone: str) -> Path:
    return topic_dir / zone / "claims.jsonl"


def _wiki_dir(topic_dir: Path) -> Path:
    return topic_dir / "final" / "wiki"


def _wiki_lock(topic_dir: Path) -> Path:
    """Use one lock for the page layer, not one per file.

    A filename is derived from the title, so concurrent writers for one concept could otherwise
    take different locks."""
    return _wiki_dir(topic_dir) / "pages"


def _concept_slug(title: str) -> str:
    """Derive a page filename from its title, retaining Unicode and replacing punctuation by hyphens."""
    slug = _CONCEPT_SLUG.sub("-", title.strip()).strip("-").lower()
    if not slug:
        raise ValueError(f"title {title!r} does not produce a concept slug for a filename")
    return slug


def _wiki_path_from_slug(topic_dir: Path, slug: str) -> Path:
    """Build a page path from an explicit slug: one dot-free component, then normal normalization."""
    if not isinstance(slug, str) or not slug:
        raise ValueError("page slug cannot be empty")
    if Path(slug).name != slug or "." in slug:
        raise ValueError(
            f"page slug {slug!r} must be one name without path components or dots")
    return _wiki_dir(topic_dir) / (_concept_slug(slug) + ".md")


def add_source(topic_dir: str | Path, *, kind: str, title: str, url: str | None = None,
               tool: str | None = None, content_path: str | None = None,
               meta: dict | None = None) -> str:
    """Idempotently return the existing id when the same source is added again."""
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    src = {"id": make_source_id(kind, url, title), "kind": kind, "title": title,
           "fetched_at": _now()}
    if url:
        src["url"] = url
    if tool:
        src["tool"] = tool
    if content_path:
        src["content_path"] = content_path
    if meta:
        src["meta"] = meta
    validate_object(src, "source")
    with _locked(_sources_path(topic_dir)):
        existing = {row["id"] for row in _read_jsonl(_sources_path(topic_dir))}
        if src["id"] not in existing:
            _append_jsonl(_sources_path(topic_dir), src)
    return src["id"]


def update_source(topic_dir: str | Path, source_id: str, *, content_path: str | None = None,
                  title: str | None = None, tool: str | None = None,
                  meta: dict | None = None) -> dict:
    """Add fields to an existing source without changing its identity fields id, kind, or URL.

    A separate operation is needed because `add_source` is idempotent by id and updates nothing
    on a repeated call. There was therefore no way to add `content_path` to a source registered
    without one. A corpus assembled outside the course pipeline, using kind=web plus an external
    transcript, reached a dead end: `verify-quotes` resolves text strictly through `content_path`
    and is blind without it. On 2026-08-20 all 618 claims in one production topic returned
    `source-unreadable`.

    content_path is checked for existence immediately. Recording a path that points nowhere would
    silently blind the quote gate again. A relative path is resolved from the topic root; an
    absolute path is used as-is, matching ext.evidence. A new title is accepted only if the
    derived source id remains unchanged. Without a URL, kind plus title defines the deterministic
    identity, so a rename would otherwise create a duplicate on the next idempotent add_source.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    updates = {k: v for k, v in (("content_path", content_path), ("title", title),
                                 ("tool", tool), ("meta", meta)) if v is not None}
    if not updates:
        raise ValueError("update_source: nothing to update - provide at least one field")
    if content_path is not None:
        probe = Path(content_path)
        if not probe.is_absolute():
            probe = topic_dir / content_path
        if not probe.is_file():
            raise ValueError(f"content_path {content_path!r} does not point to a file ({probe}) - "
                             "a source pointing nowhere would blind verify-quotes again")
    path = _sources_path(topic_dir)
    with _locked(path):
        rows = _read_jsonl(path)
        idx = next((i for i, r in enumerate(rows) if r["id"] == source_id), None)
        if idx is None:
            raise KeyError(f"source {source_id} is not present in the topic registry")
        if title is not None:
            derived_id = make_source_id(rows[idx]["kind"], rows[idx].get("url"), title)
            if derived_id != source_id:
                raise ValueError(
                    f"title {title!r} changes the deterministic identity of a source without a URL: "
                    f"{source_id} -> {derived_id}; create a new source instead of renaming it")
        row = {**rows[idx], **updates}
        validate_object(row, "source")
        rows[idx] = row
        _write_jsonl(path, rows)
    return row


def _build_claim(slug: str, zone: str, *, text: str, confidence: str, status: str,
                 evidence: list[dict], tags: list[str] | None = None,
                 run_id: str | None = None, meta: dict | None = None) -> dict:
    """Build and validate a claim row without disk access, shared by single and batch writes."""
    claim = {"id": make_claim_id(slug, text), "text": text,
             "confidence": confidence, "status": status, "evidence": evidence,
             "created_at": _now()}
    if tags:
        claim["tags"] = tags
    if run_id:
        claim["run_id"] = run_id
    if meta:
        claim["meta"] = meta
    validate_object(claim, "claim")
    if status not in _ZONE_STATUS[zone]:
        raise ValidationError([f"zone {zone}: status {status!r} is not allowed "
                               f"(allowed: {sorted(_ZONE_STATUS[zone])})"])
    return claim


def add_claim(topic_dir: str | Path, zone: str, *, text: str, confidence: str, status: str,
              evidence: list[dict], tags: list[str] | None = None, run_id: str | None = None,
              meta: dict | None = None) -> str:
    topic_dir = Path(topic_dir)
    manifest = find_topic(topic_dir)
    if zone not in _ZONE_STATUS:
        raise ValueError(f"the socket writes claims only to {sorted(_ZONE_STATUS)}, not {zone!r}")
    claim = _build_claim(manifest["slug"], zone, text=text, confidence=confidence, status=status,
                         evidence=evidence, tags=tags, run_id=run_id, meta=meta)
    known_sources = {row["id"] for row in _read_jsonl(_sources_path(topic_dir))}
    missing = sorted({e["source_id"] for e in evidence} - known_sources)
    if missing:
        raise ValidationError([f"FK: source_id {missing} is absent from sources.jsonl - "
                               "call add_source first"])
    path = _claims_path(topic_dir, zone)
    with _locked(path):
        if claim["id"] in {row["id"] for row in _read_jsonl(path)}:
            raise DuplicateClaim(claim["id"])
        _append_jsonl(path, claim)
    return claim["id"]


def add_claims(topic_dir: str | Path, zone: str, claims: list[dict]) -> list[dict]:
    """Write a batch using one lock and one file rewrite for the entire batch.

    This was motivated by a production course run where 276 claims meant 276 CLI calls. Every call
    read and rewrote the whole zone file, making work quadratic in topic size, and each call was a
    separate interruption point. Duplicates do not fail the batch: every input receives its own
    `written` or `duplicate` result for the caller to interpret. Validation and foreign-key checks
    happen before the lock opens so an invalid batch does not hold the zone. Return a list of
    {"id", "result"} records in input order.
    """
    topic_dir = Path(topic_dir)
    manifest = find_topic(topic_dir)
    if zone not in _ZONE_STATUS:
        raise ValueError(f"the socket writes claims only to {sorted(_ZONE_STATUS)}, not {zone!r}")
    if not claims:
        raise ValueError("empty batch: add_claims requires a non-empty list of claims")
    built = [_build_claim(manifest["slug"], zone, **c) for c in claims]
    known_sources = {row["id"] for row in _read_jsonl(_sources_path(topic_dir))}
    missing = sorted({e["source_id"] for c in built for e in c["evidence"]} - known_sources)
    if missing:
        raise ValidationError([f"FK: source_id {missing} is absent from sources.jsonl - call add_source first"])
    path = _claims_path(topic_dir, zone)
    out: list[dict] = []
    with _locked(path):
        rows = _read_jsonl(path)
        seen = {row["id"] for row in rows}
        for claim in built:
            if claim["id"] in seen:
                out.append({"id": claim["id"], "result": "duplicate"})
                continue
            rows.append(claim)
            seen.add(claim["id"])
            out.append({"id": claim["id"], "result": "written"})
        _write_jsonl(path, rows)
    return out


def _assert_claim_persistable(topic_dir: Path, zone: str, claim: dict) -> None:
    """Gate a claim entering a zone by schema, zone status, and foreign keys.

    promote and retract share this path so neither can bypass validation. The schema engine stays
    strict and no invalid record enters a zone.
    """
    validate_object(claim, "claim")
    if claim["status"] not in _ZONE_STATUS[zone]:
        raise ValidationError([f"zone {zone}: status {claim['status']!r} is not allowed "
                               f"(allowed: {sorted(_ZONE_STATUS[zone])})"])
    known_sources = {row["id"] for row in _read_jsonl(_sources_path(topic_dir))}
    missing = sorted({e["source_id"] for e in claim["evidence"]} - known_sources)
    if missing:
        raise ValidationError([f"FK: source_id {missing} is absent from sources.jsonl"])


def promote_claim(topic_dir: str | Path, claim_id: str) -> str:
    """Move one verified claim row from staging to final; it disappears from staging.

    Only status=verified may be promoted, never candidate or retracted. Promotion does not change
    the status because verified is valid in final. Append to final first, then remove from staging.
    An interruption between those steps duplicates the row, which a repeated promotion can repair,
    instead of losing it. If the row already exists in final, skip the append but still remove it
    from staging to reconcile a prior duplicate or interruption; final is authoritative.
    """
    return promote_claims(topic_dir, [claim_id])[0]["id"]


def promote_claims(topic_dir: str | Path, claim_ids: list[str]) -> list[dict]:
    """Promote a batch with one lock covering both zones and one rewrite of each.

    Locks are always acquired from staging to final, in the same order for every caller, so two
    concurrent promotions cannot deadlock. Each id follows the single promote_claim semantics.
    A missing claim or a status other than verified rejects the whole batch before writing because
    a partially applied batch is worse than rejection: it cannot be distinguished from an
    interruption. Return a list of {"id", "result"} records with `promoted` or `already-final`.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    if not claim_ids:
        raise ValueError("empty batch: promote_claims requires a non-empty list of ids")
    staging_path = _claims_path(topic_dir, "staging")
    final_path = _claims_path(topic_dir, "final")
    with _locked(staging_path), _locked(final_path):
        staging_rows = _read_jsonl(staging_path)
        by_id = {r["id"]: r for r in staging_rows}
        missing = [cid for cid in claim_ids if cid not in by_id]
        if missing:
            raise KeyError(f"claims {missing} are absent from staging - nothing can be promoted")
        not_verified = [cid for cid in claim_ids if by_id[cid]["status"] != "verified"]
        if not_verified:
            raise ValidationError([f"only verified claims can be promoted: {cid} has status "
                                   f"{by_id[cid]['status']!r}" for cid in not_verified])
        for cid in claim_ids:
            _assert_claim_persistable(topic_dir, "final", by_id[cid])
        final_rows = _read_jsonl(final_path)
        in_final = {r["id"] for r in final_rows}
        out: list[dict] = []
        taken = set(claim_ids)
        for cid in claim_ids:
            if cid in in_final:
                out.append({"id": cid, "result": "already-final"})
                continue
            final_rows.append(by_id[cid])
            in_final.add(cid)
            out.append({"id": cid, "result": "promoted"})
        # Write final before staging: interruption between writes duplicates a row and a repeated
        # promotion repairs it. The reverse order would lose the row.
        _write_jsonl(final_path, final_rows)
        _write_jsonl(staging_path, [r for r in staging_rows if r["id"] not in taken])
    return out


def retract_claim(topic_dir: str | Path, zone: str, claim_id: str) -> str:
    """Mark a claim retracted in place without deleting its historical tombstone row.

    This works in both zones. In final it is the only legal mutation of an existing row: under the
    append-only rule only status changes and the row remains. Repeating it on a retracted claim is
    an idempotent no-op.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    if zone not in _ZONE_STATUS:
        raise ValueError(f"the socket retracts claims only in {sorted(_ZONE_STATUS)}, not {zone!r}")
    path = _claims_path(topic_dir, zone)
    with _locked(path):
        rows = _read_jsonl(path)
        idx = next((i for i, r in enumerate(rows) if r["id"] == claim_id), None)
        if idx is None:
            raise KeyError(f"claim {claim_id} is absent from {zone}")
        if rows[idx]["status"] == "retracted":
            return claim_id
        rows[idx] = {**rows[idx], "status": "retracted"}
        _assert_claim_persistable(topic_dir, zone, rows[idx])
        _write_jsonl(path, rows)
    return claim_id


def merge_claim_evidence(topic_dir: str | Path, zone: str, claim_id: str,
                         evidence: list[dict]) -> list[dict]:
    """Append evidence to an existing claim in place without changing its id or text.

    The same fact may be found again in another source. Before this function, the socket had no way
    to connect that source: add_claim returned DuplicateClaim and direct JSONL writes were forbidden,
    so the consumer accumulated `evidence_merged_pending` records until 2026-08-23.

    The append-only invariant remains intact: the evidence list only grows and no record changes or
    disappears, keeping final auditable. The operation is legal in both zones and uses the same gates
    as add_claim: schema, zone status, and the foreign key of every new source_id. A retracted claim
    cannot grow because its tombstone reflects a human decision. Deduplication uses an exact evidence
    match over source_id, quote, stance, and locator, so retrying after an interruption returns
    `duplicate` rather than an error. The entire batch is applied or rejected before writing. Return
    a list of {"source_id", "result"} records in input order, with `added` or `duplicate`.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    if zone not in _ZONE_STATUS:
        raise ValueError(f"the socket edits claims only in {sorted(_ZONE_STATUS)}, not {zone!r}")
    if not evidence or not all(isinstance(e, dict) for e in evidence):
        raise ValueError("empty batch: merge_claim_evidence requires a non-empty evidence list")
    path = _claims_path(topic_dir, zone)
    with _locked(path):
        rows = _read_jsonl(path)
        idx = next((i for i, r in enumerate(rows) if r["id"] == claim_id), None)
        if idx is None:
            raise KeyError(f"claim {claim_id} is absent from {zone}")
        if rows[idx]["status"] == "retracted":
            raise ValidationError([f"claim {claim_id} is retracted: a tombstone cannot grow"])
        seen = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in rows[idx]["evidence"]}
        merged = list(rows[idx]["evidence"])
        out: list[dict] = []
        for entry in evidence:
            key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if key in seen:
                out.append({"source_id": entry.get("source_id"), "result": "duplicate"})
                continue
            seen.add(key)
            merged.append(entry)
            out.append({"source_id": entry.get("source_id"), "result": "added"})
        if all(o["result"] == "duplicate" for o in out):
            return out
        row = {**rows[idx], "evidence": merged}
        _assert_claim_persistable(topic_dir, zone, row)
        rows[idx] = row
        _write_jsonl(path, rows)
    return out


def set_claim_status(topic_dir: str | Path, claim_ids: list[str],
                     status: str = "verified", *,
                     locked_gate: Callable[[], None] | None = None) -> list[dict]:
    """Change staging claim status in place. Exactly one transition is currently legal:
    candidate -> verified.

    `promote` accepts only verified claims, while only a claim's creator could originally write it
    as verified. A corpus verified after initial storage therefore had no valid promotion path. In
    one production import, 1,571 claims remained candidate; working around this through
    add_claim(verified) plus retract would have created a second id across the corpus.

    Gates: operate only in staging because retract_claim owns final status changes as tombstones;
    never revive retracted claims because retraction is a human decision and reversal requires a
    new claim; apply the whole batch or reject it before writing, as promote_claims does. A claim
    already verified returns `already-verified` rather than an error, allowing safe retry after an
    interruption.

    Important: the transition alone does not prove that a claim was verified; it records the
    verification result. The CLI supplies its verbatim-quote gate through locked_gate. The callback
    runs after reading and validating the batch but before changing rows, under the same claims lock.
    An exception from the gate rejects the whole batch. This avoids importing ext.evidence back into
    store and closes the race window between quote checking and the status write.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    if status != "verified":
        raise ValueError(f"set_claim_status supports only candidate -> verified, not {status!r}")
    if not claim_ids:
        raise ValueError("empty batch: set_claim_status requires a non-empty list of ids")
    path = _claims_path(topic_dir, "staging")
    with _locked(path):
        rows = _read_jsonl(path)
        by_id = {r["id"]: i for i, r in enumerate(rows)}
        missing = [cid for cid in claim_ids if cid not in by_id]
        if missing:
            raise KeyError(f"claims {missing} are absent from staging")
        blocked = [cid for cid in claim_ids
                   if rows[by_id[cid]]["status"] not in ("candidate", "verified")]
        if blocked:
            raise ValidationError([f"illegal transition: {cid} has status "
                                   f"{rows[by_id[cid]]['status']!r}; expected candidate"
                                   for cid in blocked])
        if locked_gate is not None:
            locked_gate()
        out: list[dict] = []
        for cid in claim_ids:
            idx = by_id[cid]
            if rows[idx]["status"] == "verified":
                out.append({"id": cid, "result": "already-verified"})
                continue
            row = {**rows[idx], "status": status}
            _assert_claim_persistable(topic_dir, "staging", row)
            rows[idx] = row
            out.append({"id": cid, "result": "verified"})
        _write_jsonl(path, rows)
    return out


def _build_wiki_page(topic_dir: Path, title: str, claims: list[str]) -> dict:
    """Build and validate page frontmatter; every clm_ foreign key must exist in final."""
    manifest = find_topic(topic_dir)
    page = {"id": make_wiki_id(manifest["slug"], title), "title": title,
            "claims": list(claims), "updated_at": _now()}
    validate_object(page, "wiki")
    final_ids = {row["id"] for row in _read_jsonl(_claims_path(topic_dir, "final"))}
    missing = sorted(set(claims) - final_ids)
    if missing:
        raise ValidationError([f"FK: claims {missing} are absent from final/claims.jsonl - "
                               "wiki pages may synthesize only final claims"])
    return page


def _write_wiki_page(path: Path, page: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    tmp.write_text(render_page(page, body), "utf-8")
    os.replace(tmp, path)


def _existing_page_path(topic_dir: Path, page_id: str) -> Path | None:
    """Find an existing page file by id regardless of its filename.

    The filename derives from the title while the id derives from the normalized title. Changing
    title case or punctuation therefore changes the filename but not the id. Without an id search,
    this created a second page for the same concept and duplicated claim references without a
    validation error. Search frontmatter rather than filenames.
    """
    wiki_dir = _wiki_dir(topic_dir)
    if not wiki_dir.is_dir():
        return None
    for md in sorted(wiki_dir.glob("*.md")):
        try:
            page, _ = parse_page(md.read_text("utf-8"))
        except (FrontmatterError, UnicodeDecodeError, OSError):
            continue
        if page.get("id") == page_id:
            return md
    return None


def wiki_page_path(topic_dir: str | Path, title: str, slug: str | None = None) -> Path:
    """Choose a page path: explicit slug, existing file for the id, then a title-derived slug.

    A consumer maintaining a page map needs an explicit slug because guessing a filename from a
    non-Latin title is unreliable. In a production course run, that guess made four of six topic
    maps diverge from disk.
    """
    topic_dir = Path(topic_dir)
    if slug is not None:
        return _wiki_path_from_slug(topic_dir, slug)
    manifest = find_topic(topic_dir)
    found = _existing_page_path(topic_dir, make_wiki_id(manifest["slug"], title))
    return found or _wiki_dir(topic_dir) / (_concept_slug(title) + ".md")


def add_wiki_page(topic_dir: str | Path, *, title: str, claims: list[str],
                  body: str = "", slug: str | None = None) -> str:
    """Create final/wiki/<concept>.md; an existing page is an error handled by update_wiki_page."""
    topic_dir = Path(topic_dir)
    page = _build_wiki_page(topic_dir, title, claims)
    path = wiki_page_path(topic_dir, title, slug)
    with _locked(_wiki_lock(topic_dir)):
        if path.exists():
            raise FileExistsError(f"page already exists: {path} - use update_wiki_page")
        _write_wiki_page(path, page, body)
    return page["id"]


def update_wiki_page(topic_dir: str | Path, *, title: str, claims: list[str],
                     body: str = "", slug: str | None = None) -> str:
    """Rewrite an existing page by id; unlike claims, the wiki is a mutable layer."""
    topic_dir = Path(topic_dir)
    page = _build_wiki_page(topic_dir, title, claims)
    path = wiki_page_path(topic_dir, title, slug)
    with _locked(_wiki_lock(topic_dir)):
        if not path.exists():
            raise FileNotFoundError(
                f"no page exists to update: {path} - use add_wiki_page")
        _write_wiki_page(path, page, body)
    return page["id"]


def put_wiki_page(topic_dir: str | Path, *, title: str, claims: list[str],
                  body: str = "", slug: str | None = None) -> dict:
    """Upsert a page and return its id, path, and whether it was created or updated.

    A synthesizer should not need to know whether it is running for the first time or resuming.
    Every consumer otherwise repeated the sequence of add_wiki_page, catching FileExistsError, and
    calling update_wiki_page. Returning the path also removes the need for callers to guess a
    filename from the title.
    """
    topic_dir = Path(topic_dir)
    page = _build_wiki_page(topic_dir, title, claims)
    path = wiki_page_path(topic_dir, title, slug)
    with _locked(_wiki_lock(topic_dir)):
        created = not path.exists()
        _write_wiki_page(path, page, body)
    return {"id": page["id"], "path": str(path), "result": "created" if created else "updated"}


def delete_wiki_page(topic_dir: str | Path, *, slug: str) -> dict:
    """Delete final/wiki/<slug>.md without changing claims.

    The result is idempotent: `deleted` if a file existed and `missing` if it was already absent.
    Every page operation shares the same wiki lock. A resolved-path check remains as a separate
    defense on top of the slug guard and prevents deleting a symlink target outside final/wiki.
    """
    topic_dir = Path(topic_dir)
    find_topic(topic_dir)
    path = _wiki_path_from_slug(topic_dir, slug)
    with _locked(_wiki_lock(topic_dir)):
        wiki_dir = _wiki_dir(topic_dir).resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(wiki_dir)
        except ValueError:
            raise ValueError(
                f"refusing to delete a page outside final/wiki: {resolved}") from None
        try:
            path.unlink()
        except FileNotFoundError:
            result = "missing"
        else:
            result = "deleted"
    return {"path": str(path), "result": result}


def _rows_or_error(path: Path, where: str, errors: list[str]) -> list[dict]:
    """Read for validation, returning damage as findings while retaining valid rows.

    Return the valid rows rather than an empty list because they maintain known/final_ids and
    prevent false foreign-key findings from obscuring the real damage.
    """
    rows, bad = _scan_jsonl(path)
    errors.extend(f"{where}: {b}" for b in bad)
    return rows


def validate_topic(topic_dir: str | Path) -> list[str]:
    """Validate a topic on disk: schemas, zone statuses, foreign keys, and duplicates; [] is clean."""
    topic_dir = Path(topic_dir)
    errors: list[str] = []
    try:
        find_topic(topic_dir)
    except (OSError, ValueError, ValidationError) as e:
        return [f"topic.json: {e}"]
    sources = _rows_or_error(_sources_path(topic_dir), "sources", errors)
    known: set[str] = set()
    for i, row in enumerate(sources):
        try:
            validate_object(row, "source")
        except ValidationError as e:
            errors.append(f"sources[{i}]: {e}")
        known.add(row.get("id", ""))
    final_ids: set[str] = set()
    for zone in ("staging", "final"):
        seen: set[str] = set()
        for i, row in enumerate(_rows_or_error(_claims_path(topic_dir, zone),
                                               f"{zone}/claims", errors)):
            where = f"{zone}/claims[{i}]"
            if zone == "final":
                final_ids.add(row.get("id", ""))
            try:
                validate_object(row, "claim")
            except ValidationError as e:
                errors.append(f"{where}: {e}")
                continue
            if row["status"] not in _ZONE_STATUS[zone]:
                errors.append(f"{where}: status {row['status']!r} is not allowed in this zone")
            for e in row["evidence"]:
                if e["source_id"] not in known:
                    errors.append(f"{where}: FK {e['source_id']} is absent from sources.jsonl")
            if row["id"] in seen:
                errors.append(f"{where}: duplicate id {row['id']}")
            seen.add(row["id"])
    _validate_wiki(topic_dir, final_ids, errors)
    return errors


def _validate_wiki(topic_dir: Path, final_ids: set[str], errors: list[str]) -> None:
    wiki_dir = _wiki_dir(topic_dir)
    try:
        with os.scandir(wiki_dir) as entries:
            pages = sorted(wiki_dir / entry.name for entry in entries
                           if entry.name.endswith(".md"))
    except FileNotFoundError:
        return
    except OSError as e:
        errors.append(f"final/wiki: directory is unreadable ({type(e).__name__}: {e})")
        return
    seen: set[str] = set()
    for md in pages:
        where = f"final/wiki/{md.name}"
        try:
            page, _body = parse_page(md.read_text("utf-8"))
        except (FrontmatterError, UnicodeDecodeError, OSError) as e:
            errors.append(f"{where}: {e}")
            continue
        try:
            validate_object(page, "wiki")
        except ValidationError as e:
            errors.append(f"{where}: {e}")
            continue
        for cid in page["claims"]:
            if cid not in final_ids:
                errors.append(f"{where}: FK {cid} is absent from final/claims.jsonl")
        if page["id"] in seen:
            errors.append(f"{where}: duplicate id {page['id']}")
        seen.add(page["id"])
