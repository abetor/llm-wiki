"""llm-wiki socket CLI for non-Python mutators; Python consumers call the library in process.

The tool owns no data. A topic is an external directory whose path is passed to every subcommand.
The CLI therefore has no --home option because it has nothing to place in a tool home.

Exit codes follow the workspace contract: EXIT_OK=0 for success and EXIT_FAIL=1 for validation,
foreign-key, duplicate, configuration, or usage failures. The offline validator never returns
75 or 111 because it has no network, quotas, or transient remote failures. Callers distinguish
outcomes through stderr: a duplicate is printed as "duplicate: <id>" and every other error as
"error: <message naming the violated rule>".

The parser uses shared.cliargs.ArgumentParser instead of stock argparse. Stock argparse exits with
2 for invalid input such as an unknown subcommand, bad zone, or missing argument, but that code is
outside the contract. Subparsers automatically inherit the custom class.
"""
import json
import sys

from . import store, topic, validate
from .ext import evidence, query
from .shared.cliargs import ArgumentParser
from .validate import SpecError, load_schema

EXIT_OK = 0    # Success.
EXIT_FAIL = 1  # Validation, foreign-key, duplicate, or usage failure; 2 is outside the contract.

_CORE_SCHEMAS = ("topic", "source", "claim", "wiki")  # Without these, no specification exists.


def main(argv: list[str] | None = None) -> int:
    p = ArgumentParser(prog="llmwiki", description="llm-wiki knowledge socket")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="check that the engine understands every schema; never mutate")
    # The schema engine does not need a topic path, but doctor accepts it and verifies that it is a
    # topic. New consumers commonly try `doctor .` first; rejecting the extra argument looked like
    # a broken socket in a production run and affected two of six topics.
    sp.add_argument("topic_dir", nargs="?")

    sp = sub.add_parser("init", help="create a topic")
    sp.add_argument("base"), sp.add_argument("topic")
    sp.add_argument("--slug"), sp.add_argument("--description", default="")

    sp = sub.add_parser("add-source", help="add a source from JSON on stdin")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("add-claim", help="add a claim from JSON on stdin; --batch accepts an array")
    sp.add_argument("topic_dir"), sp.add_argument("zone", choices=["staging", "final"])
    sp.add_argument("--batch", action="store_true",
                    help="read an array of claims from stdin and write the file once; "
                         "duplicates do not fail the batch and each result appears in JSON")

    sp = sub.add_parser("promote", help="move a verified claim row from staging to final")
    sp.add_argument("topic_dir"), sp.add_argument("claim_id", nargs="?")
    sp.add_argument("--batch", action="store_true",
                    help="read an id array or one id per line from stdin instead of claim_id")

    sp = sub.add_parser("retract", help="mark a claim retracted while retaining its history row")
    sp.add_argument("topic_dir"), sp.add_argument("zone", choices=["staging", "final"])
    sp.add_argument("claim_id")

    sp = sub.add_parser("merge-evidence",
                        help="append evidence to an existing claim from a JSON array on stdin")
    sp.add_argument("topic_dir"), sp.add_argument("zone", choices=["staging", "final"])
    sp.add_argument("claim_id")

    sp = sub.add_parser("set-status", help="staging candidate -> verified with a quote gate by default")
    sp.add_argument("topic_dir"), sp.add_argument("claim_id", nargs="?")
    sp.add_argument("--status", choices=["verified"], default="verified")
    sp.add_argument("--batch", action="store_true",
                    help="read an id array or one id per line from stdin instead of claim_id")
    sp.add_argument("--skip-quote-check", action="store_true",
                    help="do not compare evidence.quote with its source before transition; "
                         "use only when another harness has already checked exact quotes")

    sp = sub.add_parser("update-source",
                        help="add fields to an existing source without changing id, kind, or URL")
    sp.add_argument("topic_dir"), sp.add_argument("source_id")
    sp.add_argument("--content-path", help="source text path, relative to the topic root or absolute")
    sp.add_argument("--title", help="new title, allowed only if the deterministic source id stays unchanged")
    sp.add_argument("--tool")

    sp = sub.add_parser("add-wiki", help="create a wiki page from title, claims, and body JSON on stdin")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("update-wiki", help="rewrite a wiki page from JSON on stdin")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("put-wiki", help="upsert a wiki page by updating an existing page or creating it")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("delete-page", help="delete final/wiki/<slug>.md without changing claims")
    sp.add_argument("topic_dir"), sp.add_argument("slug")

    sp = sub.add_parser("validate", help="validate a complete topic: schemas, foreign keys, and duplicates")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("list-claims", help="query claims as read-only JSON")
    sp.add_argument("topic_dir")
    sp.add_argument("--zone", choices=["staging", "final", "all"], default="all")
    sp.add_argument("--tag"), sp.add_argument("--source"), sp.add_argument("--contains")
    sp.add_argument("--fields", default="id,zone,status,text",
                    help="comma-separated fields; '*' returns complete rows")
    sp.add_argument("--limit", type=int)

    sp = sub.add_parser("list-sources", help="return the source registry as read-only JSON")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("list-wiki", help="return synthesis pages with slug, path, claims, and size as JSON")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("stats", help="return the topic funnel from sources to claims to pages as JSON")
    sp.add_argument("topic_dir")

    sp = sub.add_parser("verify-quotes",
                        help="check every evidence.quote verbatim against source text and its locator")
    sp.add_argument("topic_dir")
    sp.add_argument("--zone", choices=["staging", "final", "all"], default="all")

    sp = sub.add_parser("audit", help="audit synthesis completeness: orphans, density, and staging")
    sp.add_argument("topic_dir")
    sp.add_argument("--min-words-per-claim", type=float, default=60.0)
    sp.add_argument("--max-words-per-claim", type=float, default=250.0)
    sp.add_argument("--no-anchors", action="store_true",
                    help="do not require clm_... markers in page bodies")

    a = p.parse_args(argv)
    try:
        if a.cmd == "doctor":
            return doctor(a.topic_dir)
        if a.cmd == "init":
            root = topic.init_topic(a.base, a.topic, slug=a.slug, description=a.description)
            print(root)
        elif a.cmd == "add-source":
            print(store.add_source(a.topic_dir, **json.load(sys.stdin)))
        elif a.cmd == "add-claim":
            if a.batch:
                batch = json.load(sys.stdin)
                if not isinstance(batch, list):
                    raise ValueError("--batch expects an array of claims on stdin")
                print(query.as_json(store.add_claims(a.topic_dir, a.zone, batch)))
            else:
                print(store.add_claim(a.topic_dir, a.zone, **json.load(sys.stdin)))
        elif a.cmd == "promote":
            if a.batch:
                print(query.as_json(store.promote_claims(a.topic_dir, _read_ids(sys.stdin))))
            elif a.claim_id:
                print(store.promote_claim(a.topic_dir, a.claim_id))
            else:
                raise ValueError("promote requires claim_id or --batch with ids on stdin")
        elif a.cmd == "retract":
            print(store.retract_claim(a.topic_dir, a.zone, a.claim_id))
        elif a.cmd == "merge-evidence":
            batch = json.load(sys.stdin)
            if not isinstance(batch, list):
                raise ValueError("merge-evidence expects an evidence array on stdin")
            print(query.as_json(store.merge_claim_evidence(a.topic_dir, a.zone, a.claim_id, batch)))
        elif a.cmd == "set-status":
            ids = _read_ids(sys.stdin) if a.batch else ([a.claim_id] if a.claim_id else [])
            if not ids:
                raise ValueError("set-status requires claim_id or --batch with ids on stdin")
            locked_gate = None if a.skip_quote_check else lambda: _require_quotes(a.topic_dir, ids)
            print(query.as_json(store.set_claim_status(
                a.topic_dir, ids, a.status, locked_gate=locked_gate)))
        elif a.cmd == "update-source":
            print(query.as_json(store.update_source(
                a.topic_dir, a.source_id, content_path=a.content_path,
                title=a.title, tool=a.tool)))
        elif a.cmd == "add-wiki":
            print(store.add_wiki_page(a.topic_dir, **json.load(sys.stdin)))
        elif a.cmd == "update-wiki":
            print(store.update_wiki_page(a.topic_dir, **json.load(sys.stdin)))
        elif a.cmd == "put-wiki":
            print(query.as_json(store.put_wiki_page(a.topic_dir, **json.load(sys.stdin))))
        elif a.cmd == "delete-page":
            print(query.as_json(store.delete_wiki_page(a.topic_dir, slug=a.slug)))
        elif a.cmd == "validate":
            errors = store.validate_topic(a.topic_dir)
            for e in errors:
                print(e, file=sys.stderr)
            print("OK" if not errors else f"{len(errors)} errors")
            return EXIT_FAIL if errors else EXIT_OK
        elif a.cmd == "list-claims":
            print(query.as_json(query.list_claims(
                a.topic_dir, zone=a.zone, tag=a.tag, source=a.source, contains=a.contains,
                fields=[f.strip() for f in a.fields.split(",") if f.strip()], limit=a.limit)))
        elif a.cmd == "list-sources":
            print(query.as_json(query.read_sources(a.topic_dir)))
        elif a.cmd == "list-wiki":
            print(query.as_json(query.read_pages(a.topic_dir)))
        elif a.cmd == "stats":
            print(query.as_json(query.stats(a.topic_dir)))
        elif a.cmd == "verify-quotes":
            findings = evidence.verify_quotes(a.topic_dir, zone=a.zone)
            print(query.as_json(findings))
            return EXIT_FAIL if any(f["level"] == "error" for f in findings) else EXIT_OK
        elif a.cmd == "audit":
            findings = query.audit(a.topic_dir,
                                   min_words_per_claim=a.min_words_per_claim,
                                   max_words_per_claim=a.max_words_per_claim,
                                   require_anchors=not a.no_anchors)
            print(query.as_json(findings))
            # Exit 1 only for errors. A warning deserves review but does not fail the gate.
            return EXIT_FAIL if any(f["level"] == "error" for f in findings) else EXIT_OK
    except store.DuplicateClaim as e:
        print(f"duplicate: {e}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as e:  # Reject invalid input with a useful message instead of a traceback.
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _read_ids(stream) -> list[str]:
    """Parse batch ids from a JSON array or one id per line.

    Callers use both forms: scripts produce the first and pipelines produce the second. Supporting
    both centrally is inexpensive and avoids duplicating format detection in every caller.
    """
    raw = stream.read().strip()
    if not raw:
        raise ValueError("--batch: stdin is empty, so there is nothing to promote")
    if raw.startswith("["):
        ids = json.loads(raw)
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("--batch: expected an array of string ids")
        return ids
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _require_quotes(topic_dir: str, claim_ids: list[str]) -> None:
    """Gate only the selected batch; store invokes this under the staging claims lock."""
    wanted = set(claim_ids)
    bad = sorted({f["claim"] for f in evidence.verify_quotes(topic_dir, zone="staging")
                  if f["level"] == "error" and f.get("claim") in wanted})
    if bad:
        raise ValueError(
            f"quote gate failed for {len(bad)} claims in the batch: {bad[:5]}"
            + ("..." if len(bad) > 5 else "")
            + ". Resolve verify-quotes findings, or if another harness already checked exact "
              "quotes, retry with --skip-quote-check")


def _spec_names() -> list[str]:
    """Read schema names from disk instead of a hard-coded list.

    A list would require manual updates. A forgotten schema would bypass the gate, leaving a rule
    in the specification that the engine silently failed to enforce. Core schema names are always
    included so a missing base schema remains a failure instead of looking like nothing to check.
    """
    suffix = ".schema.json"
    found = {p.name[: -len(suffix)] for p in validate.SPEC_DIR.glob("*" + suffix)}
    return sorted(found | set(_CORE_SCHEMAS))


def doctor(topic_dir: str | None = None) -> int:
    """Diagnose without mutation; the fail-closed engine must understand every schema.

    A schema that raises SpecError contains a rule the engine would otherwise fail to enforce.
    doctor catches it before anything is written. An optional topic path is checked separately by
    reading and validating its manifest, so one command answers whether both the socket and this
    topic directory are valid.
    """
    names = _spec_names()
    problems: list[str] = []
    for name in names:
        try:
            load_schema(name)
        except (SpecError, OSError, ValueError) as e:
            problems.append(f"schema {name}: {e}")
    if topic_dir is not None:
        try:
            manifest = topic.find_topic(topic_dir)
        except Exception as e:
            problems.append(f"topic {topic_dir}: {e}")
        else:
            print(f"  topic {topic_dir}: slug={manifest['slug']} "
                  f"spec_version={manifest['spec_version']}")
    print("llmwiki doctor: " + ("READY" if not problems else "NOT READY"))
    print("  specification schemas understood by the fail-closed engine: "
          + ", ".join(n + ".schema.json" for n in names))
    for pr in problems:
        print("  problem: " + pr)
    return EXIT_OK if not problems else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
