#!/usr/bin/env python3
"""Live socket-contract check against the installed package as a consumer sees it.

This is separate from tests because tests run at the repository root and exercise the local source,
while consumers install the distribution and import llmwiki through a different path. A broken
pyproject can fail only that path, for example by omitting spec/*.json or packaging the wrong module.
The check uses only the public API and real files in a temporary directory. It uses no network, LLM,
or external data.

Run from any directory except the repository root, where local source would shadow the installed
package: python3 smoke/smoke_contract.py
Exit codes: 0 means the contract holds, 1 means a step failed, and 77 means llmwiki is not installed.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import llmwiki
    import llmwiki.validate as V
    from llmwiki import (DuplicateClaim, add_claim, add_source, add_wiki_page,
                         init_topic, promote_claim, retract_claim,
                         update_wiki_page, validate_topic)
    from llmwiki.validate import SpecError, ValidationError, load_schema
except ImportError as e:
    # Skip only when the package is entirely absent. If it is found but an internal import fails,
    # such as from a missing module, bad installation, or pyproject typo, the exact target of this
    # smoke is defective. Calling that a skip would report green over a dead package.
    if isinstance(e, ModuleNotFoundError) and e.name == "llmwiki":
        print(f"skip: llmwiki is not importable ({e}); run pip install -e <repository>")
        sys.exit(77)
    print(f"FAIL: llmwiki was found but its import is broken - {type(e).__name__}: {e}")
    sys.exit(1)

ROWS: list[tuple[str, str, str, str]] = []
FAILED = 0


def step(name: str, expect: str, fn) -> None:
    global FAILED
    try:
        fact, ok = fn()
    except Exception as e:  # An uncaught exception is a reported fact, not a smoke crash.
        fact, ok = f"UNEXPECTED {type(e).__name__}: {e}", False
    if not ok:
        FAILED += 1
    ROWS.append((name, expect, str(fact)[:160], "OK" if ok else "FAIL"))


def rows_of(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def raises(exc, fn):
    """Return exception text and True when expected; otherwise return the observed result and False."""
    try:
        r = fn()
    except exc as e:
        return f"{type(e).__name__}: {e}", True
    except Exception as e:
        return f"different exception {type(e).__name__}: {e}", False
    return f"DID NOT RAISE, returned {r!r}", False


base = Path(tempfile.mkdtemp(prefix="llmwiki-smoke-"))
T: dict = {}
print(f"package: {llmwiki.__file__}")
print(f"specification version: {llmwiki.SPEC_VERSION}")
print(f"temporary base: {base}\n")


# --- topic -----------------------------------------------------------------
def s_init():
    root = init_topic(base, "Contract Smoke")
    T["root"] = root
    zones = sorted(p.name for p in root.iterdir() if p.is_dir())
    m = json.loads((root / "topic.json").read_text("utf-8"))
    return (f"{root.name}, zones={zones}, spec_version={m['spec_version']}",
            m["slug"] == "contract-smoke" and zones == ["final", "sources", "staging", "work"])


step("init_topic", "topic directory plus work/staging/final/sources and a valid manifest", s_init)
step("init_topic repeated", "FileExistsError rather than overwrite",
     lambda: raises(FileExistsError, lambda: init_topic(base, "Contract Smoke")))


# --- sources ---------------------------------------------------------------
def s_src():
    T["src"] = add_source(T["root"], kind="web", title="Article", url="https://example.org/a")
    return T["src"], T["src"].startswith("src_") and len(T["src"]) == 16


def s_src_idem():
    again = add_source(T["root"], kind="web", title="A DIFFERENT title",
                       url="https://example.org/a")
    n = len(rows_of(T["root"] / "sources/sources.jsonl"))
    return f"same id={again == T['src']}, rows={n}", again == T["src"] and n == 1


step("add_source", "src_<12hex> id and one registry row", s_src)
step("add_source same URL", "same id and no duplicate row, proving idempotence", s_src_idem)

EV = lambda: [{"source_id": T["src"], "quote": "quote", "stance": "supports"}]  # noqa: E731


# --- claims: deduplication, foreign keys, schema, and zone -----------------
def s_claim():
    T["c1"] = add_claim(T["root"], "staging", text="First topic fact", confidence="high",
                        status="candidate", evidence=EV())
    return T["c1"], T["c1"].startswith("clm_") and len(T["c1"]) == 16


step("add_claim(staging, candidate)", "clm_<12hex> id and one staging row", s_claim)
step("add_claim same text with different case and whitespace",
     "DuplicateClaim from exact deduplication of normalized text",
     lambda: raises(DuplicateClaim, lambda: add_claim(
         T["root"], "staging", text="  FIRST   topic fact ", confidence="low",
         status="candidate", evidence=EV())))
step("add_claim with missing source_id", "ValidationError FK and no dangling reference written",
     lambda: raises(ValidationError, lambda: add_claim(
         T["root"], "staging", text="dangling reference", confidence="low", status="candidate",
         evidence=[{"source_id": "src_000000000000", "quote": "q", "stance": "supports"}])))
step("add_claim with confidence outside enum", "schema ValidationError without silent repair",
     lambda: raises(ValidationError, lambda: add_claim(
         T["root"], "staging", text="invalid confidence", confidence="very high",
         status="candidate", evidence=EV())))
step("add_claim(staging, status=final)", "zone-gate ValidationError",
     lambda: raises(ValidationError, lambda: add_claim(
         T["root"], "staging", text="wrong zone", confidence="high", status="final",
         evidence=EV())))


# --- promotion -------------------------------------------------------------
step("promote candidate claim", "ValidationError because only verified claims can be promoted",
     lambda: raises(ValidationError, lambda: promote_claim(T["root"], T["c1"])))


def s_promote():
    cid = add_claim(T["root"], "staging", text="Verified fact", confidence="medium",
                    status="verified", evidence=EV())
    T["c2"] = cid
    promote_claim(T["root"], cid)
    staging = [r["id"] for r in rows_of(T["root"] / "staging/claims.jsonl")]
    final = rows_of(T["root"] / "final/claims.jsonl")
    return (f"staging {staging}, final {[r['id'] for r in final]}, "
            f"status={final[0]['status']}",
            cid not in staging and [r["id"] for r in final] == [cid]
            and final[0]["status"] == "verified")


step("promote verified claim",
     "row moved from staging to final without a status change", s_promote)
step("promote missing id", "KeyError because there is nothing to promote",
     lambda: raises(KeyError, lambda: promote_claim(T["root"], "clm_000000000000")))


# --- wiki ------------------------------------------------------------------
def s_wiki():
    T["wid"] = add_wiki_page(T["root"], title="Concept", claims=[T["c2"]], body="# Concept\n\nbody")
    page = (T["root"] / "final/wiki/concept.md")
    return (f"{T['wid']}, file={page.name}, frontmatter={page.read_text('utf-8').startswith('---')}",
            T["wid"].startswith("wik_") and page.exists())


step("add_wiki_page with final claim", "wik_<12hex> and final/wiki/<concept>.md with frontmatter", s_wiki)
step("add_wiki_page with staging claim", "ValidationError FK because synthesis uses final only",
     lambda: raises(ValidationError, lambda: add_wiki_page(
         T["root"], title="Outside final", claims=[T["c1"]], body="x")))
step("add_wiki_page repeated", "FileExistsError; use update_wiki_page",
     lambda: raises(FileExistsError, lambda: add_wiki_page(
         T["root"], title="Concept", claims=[T["c2"]], body="y")))


def s_update():
    update_wiki_page(T["root"], title="Concept", claims=[T["c2"]], body="rewritten")
    return ("body was updated",
            "rewritten" in (T["root"] / "final/wiki/concept.md").read_text("utf-8"))


step("update_wiki_page", "page rewrite in the mutable wiki layer", s_update)


# --- tombstone -------------------------------------------------------------
def s_retract():
    before = rows_of(T["root"] / "final/claims.jsonl")
    retract_claim(T["root"], "final", T["c2"])
    after = rows_of(T["root"] / "final/claims.jsonl")
    return (f"rows before {len(before)}, after {len(after)}, status={after[0]['status']}",
            len(before) == len(after) and after[0]["status"] == "retracted"
            and after[0]["text"] == before[0]["text"])


step("retract_claim(final)", "status becomes retracted and the history row remains", s_retract)


def s_retract_idem():
    retract_claim(T["root"], "final", T["c2"])
    return ("repeated call makes no changes", len(rows_of(T["root"] / "final/claims.jsonl")) == 1)


step("retract_claim repeated", "idempotent no-op", s_retract_idem)


# --- topic validation and damaged input -----------------------------------
step("validate_topic on a clean topic", "empty error list",
     lambda: (validate_topic(T["root"]), validate_topic(T["root"]) == []))


def corrupted(mutate):
    """Corrupt a copy and validate it, leaving the original topic clean."""
    dst = base / "corrupt"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(T["root"], dst)
    mutate(dst)
    return validate_topic(dst)


def s_bad_conf():
    def m(d):
        rows = rows_of(d / "final/claims.jsonl")
        rows[0]["confidence"] = "extreme"
        (d / "final/claims.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    errs = corrupted(m)
    return errs, any("confidence" in e for e in errs)


def s_bad_fk():
    errs = corrupted(lambda d: (d / "sources/sources.jsonl").write_text("", "utf-8"))
    return errs, any("FK" in e for e in errs)


def s_bad_page():
    errs = corrupted(lambda d: (d / "final/wiki/concept.md").write_text("not a page\n", "utf-8"))
    return errs, any("wiki" in e for e in errs)


def s_bad_manifest():
    errs = corrupted(lambda d: (d / "topic.json").write_text('{"spec_version": "9.9.9"}', "utf-8"))
    return errs, any("topic.json" in e for e in errs)


def s_corrupt_line():
    errs = corrupted(lambda d: (d / "final/claims.jsonl").write_text('{truncated\n', "utf-8"))
    return errs, any("claims.jsonl:1" in e for e in errs)


step("manual confidence corruption in final", "validate_topic catches edits outside the socket", s_bad_conf)
step("emptied source registry", "validate_topic catches a dangling foreign key", s_bad_fk)
step("wiki page replaced by non-page text", "validate_topic catches broken frontmatter", s_bad_page)
step("broken topic manifest", "validate_topic reports it and stops deeper checks", s_bad_manifest)
step("truncated row in final/claims.jsonl",
     "validate_topic reports file and line instead of raising a traceback", s_corrupt_line)


# --- fail-closed schema engine --------------------------------------------
def s_fail_closed():
    spec = base / "spec-mutant"
    shutil.rmtree(spec, ignore_errors=True)
    spec.mkdir()
    for n in ("topic", "source", "claim", "wiki"):
        src = V.SPEC_DIR / f"{n}.schema.json"
        (spec / src.name).write_text(src.read_text("utf-8"), "utf-8")
    bad = spec / "claim.schema.json"
    bad.write_text(bad.read_text("utf-8").replace('"type": "object"',
                                                  '"type": "object", "if": {}', 1), "utf-8")
    real, V.SPEC_DIR = V.SPEC_DIR, spec
    try:
        return raises(SpecError, lambda: load_schema("claim"))
    finally:
        V.SPEC_DIR = real


step("schema with a keyword unknown to the engine",
     "SpecError fail-closed rather than silently ignoring a rule", s_fail_closed)


def s_not_a_topic():
    """A failure must name its cause because any generic exception could merely be a defect."""
    fact, ok = raises(FileNotFoundError, lambda: add_source(base / "not-a-topic", kind="web",
                                                            title="x", url="https://x"))
    return fact, ok and "topic.json" in fact


step("write into directory without topic.json", "FileNotFoundError identifies topic.json",
     s_not_a_topic)


# --- report ----------------------------------------------------------------
head = ("step", "expectation", "fact", "result")
w = [max(len(str(r[i])) for r in [*ROWS, head]) for i in range(4)]
print("| " + " | ".join(head[i].ljust(w[i]) for i in range(4)) + " |")
print("|" + "|".join("-" * (x + 2) for x in w) + "|")
for r in ROWS:
    print("| " + " | ".join(str(r[i]).ljust(w[i]) for i in range(4)) + " |")
print(f"\nsteps: {len(ROWS)}, failures: {FAILED}")
print("untested boundaries: concurrent writes, power loss between zones during file-by-file "
      "promotion, corpora larger than hundreds of rows, and external mutators.")
shutil.rmtree(base, ignore_errors=True)
sys.exit(1 if FAILED else 0)
