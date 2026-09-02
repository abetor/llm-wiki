import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import (DuplicateClaim, add_claim, add_source, add_wiki_page,  # noqa: E402
                     init_topic, make_claim_id, validate_topic)
from llmwiki.store import CorruptLine  # noqa: E402
from llmwiki.validate import SpecError, ValidationError, load_schema  # noqa: E402


@pytest.fixture
def topic_dir(tmp_path):
    root = init_topic(tmp_path, "Тестовая Тема про LLM")
    return root


def _src(topic_dir):
    return add_source(topic_dir, kind="web", title="Article", url="https://example.com/a")


def _claim_kw(src_id, **over):
    kw = dict(text="LLMs consume tokens", confidence="high", status="verified",
              evidence=[{"source_id": src_id, "quote": "tokens", "stance": "supports"}])
    kw.update(over)
    return kw


def test_init_topic_structure(topic_dir):
    assert (topic_dir / "topic.json").exists()
    for z in ("work", "staging", "final", "sources/content", "final/wiki"):
        assert (topic_dir / z).is_dir()
    assert topic_dir.name == "llm"  # The slugger deliberately drops Cyrillic characters.
    with pytest.raises(FileExistsError):
        init_topic(topic_dir.parent, "Тестовая Тема про LLM")


def test_source_idempotent(topic_dir):
    a, b = _src(topic_dir), _src(topic_dir)
    assert a == b
    rows = (topic_dir / "sources" / "sources.jsonl").read_text().splitlines()
    assert len(rows) == 1


def test_claim_roundtrip_and_dup(topic_dir):
    src = _src(topic_dir)
    cid = add_claim(topic_dir, "final", **_claim_kw(src))
    assert cid == make_claim_id("llm", "LLMs consume tokens")
    with pytest.raises(DuplicateClaim):
        add_claim(topic_dir, "final", **_claim_kw(src))
    # The same text with different whitespace and case yields the same id and is a duplicate.
    with pytest.raises(DuplicateClaim):
        add_claim(topic_dir, "final", **_claim_kw(src, text="  llms  consume tokens "))
    assert validate_topic(topic_dir) == []


def test_zone_status_rules(topic_dir):
    src = _src(topic_dir)
    with pytest.raises(ValidationError):
        add_claim(topic_dir, "final", **_claim_kw(src, status="candidate"))
    add_claim(topic_dir, "staging", **_claim_kw(src, status="candidate"))
    with pytest.raises(ValueError):
        add_claim(topic_dir, "work", **_claim_kw(src))


def test_fk_enforced(topic_dir):
    with pytest.raises(ValidationError, match="FK"):
        add_claim(topic_dir, "final",
                  **_claim_kw("src_000000000000"))


def test_schema_rejects_garbage(topic_dir):
    src = _src(topic_dir)
    with pytest.raises(ValidationError):
        add_claim(topic_dir, "final", **_claim_kw(src, confidence="probably"))
    with pytest.raises(ValidationError):
        add_claim(topic_dir, "final", **_claim_kw(src, evidence=[]))


def test_validate_topic_catches_hand_edits(topic_dir):
    src = _src(topic_dir)
    add_claim(topic_dir, "final", **_claim_kw(src))
    path = topic_dir / "final" / "claims.jsonl"
    row = json.loads(path.read_text())
    row["confidence"] = "absolutely"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    assert validate_topic(topic_dir)


@pytest.mark.parametrize("rel", ["sources/sources.jsonl", "staging/claims.jsonl",
                                 "final/claims.jsonl"])
@pytest.mark.parametrize("junk", ['{truncated row', '[1, 2]', '"string, not an object"'],
                         ids=["not-json", "json-array", "json-string"])
def test_validate_reports_corrupt_line_instead_of_crashing(topic_dir, rel, junk):
    # Manual corruption outside the socket is exactly what validate_topic must detect. It used to
    # raise JSONDecodeError or AttributeError and break the list[str] contract for CLI callers.
    src = _src(topic_dir)
    add_claim(topic_dir, "staging", **_claim_kw(src, status="candidate"))
    add_claim(topic_dir, "final", **_claim_kw(src, text="final fact"))
    path = topic_dir / rel
    path.write_text(path.read_text("utf-8") + junk + "\n", "utf-8")  # Valid row 1, damage on row 2.
    errors = validate_topic(topic_dir)  # Verify a return value, not an exception.
    assert any(f"{path.name}:2" in e for e in errors), errors
    assert not any(f"{path.name}:{n}" in e for n in (1, 3) for e in errors), errors


@pytest.mark.parametrize("pos", [1, 2, 3], ids=["first", "middle", "last"])
@pytest.mark.parametrize("junk", ['{truncated row', '[1, 2]', '"string, not an object"',
                                  "", "   "],
                         ids=["not-json", "json-array", "json-string", "empty", "spaces"])
def test_corrupt_line_number_is_exact(topic_dir, pos, junk):
    # Finding line numbers guide manual repair. Damage is placed at different positions so a mutant
    # returning a constant line number such as 1, 3, or 99 must fail.
    src = _src(topic_dir)
    add_claim(topic_dir, "final", **_claim_kw(src, text="fact one"))
    add_claim(topic_dir, "final", **_claim_kw(src, text="fact two"))
    p = topic_dir / "final" / "claims.jsonl"
    lines = p.read_text("utf-8").splitlines()
    lines.insert(pos - 1, junk)
    p.write_text("".join(x + "\n" for x in lines), "utf-8")
    errors = validate_topic(topic_dir)
    assert any(f"claims.jsonl:{pos}" in e for e in errors), errors
    assert not any(f"claims.jsonl:{n}" in e for n in {1, 2, 3} - {pos} for e in errors), errors


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_blank_line_in_jsonl_is_corruption_not_silent_skip(topic_dir, blank):
    # An empty row inside JSONL is corruption, not nothing to read. The file was edited outside the
    # socket and the next complete rewrite would silently erase the row.
    src = _src(topic_dir)
    add_claim(topic_dir, "final", **_claim_kw(src))
    p = topic_dir / "final" / "claims.jsonl"
    p.write_text(p.read_text("utf-8") + blank + "\n", "utf-8")
    assert any("claims.jsonl:2" in e for e in validate_topic(topic_dir))
    with pytest.raises(CorruptLine, match="claims.jsonl:2"):
        add_claim(topic_dir, "final", **_claim_kw(src, text="second fact"))


def test_empty_jsonl_file_is_legal(topic_dir):
    # Zero bytes means no rows, exactly how a newly created zone looks, and is not corruption.
    src = _src(topic_dir)
    (topic_dir / "final" / "claims.jsonl").write_text("", "utf-8")
    assert validate_topic(topic_dir) == []
    add_claim(topic_dir, "final", **_claim_kw(src))


def test_unicode_line_separators_roundtrip_escaped(topic_dir):
    src = _src(topic_dir)
    text = "Кириллица\u0085строка\u2028абзац\u2029конец"
    add_claim(topic_dir, "final", **_claim_kw(src, text=text))
    raw = (topic_dir / "final" / "claims.jsonl").read_text("utf-8")

    assert "Кириллица" in raw  # The test verifies that ensure_ascii=False preserves Cyrillic text.
    assert "\u0085" in raw  # U+0085 is valid inside a JSON string and does not separate records.
    assert "\u2028" not in raw and "\u2029" not in raw
    assert "\\u2028" in raw and "\\u2029" in raw
    assert json.loads(raw)["text"] == text
    assert validate_topic(topic_dir) == []


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"],
                         ids=["u0085", "u2028", "u2029"])
def test_unicode_separator_between_json_objects_is_corrupt_line(topic_dir, separator):
    _src(topic_dir)
    path = topic_dir / "sources" / "sources.jsonl"
    row = path.read_text("utf-8").rstrip("\n")
    path.write_text(row + separator + row + "\n", "utf-8")

    errors = validate_topic(topic_dir)
    assert any("sources.jsonl:1" in e for e in errors), errors
    with pytest.raises(CorruptLine, match="sources.jsonl:1"):
        add_source(topic_dir, kind="web", title="Another", url="https://example.com/b")


def test_jsonl_accepts_crlf_but_not_bare_cr_as_record_separator(topic_dir):
    _src(topic_dir)
    path = topic_dir / "sources" / "sources.jsonl"
    row = path.read_text("utf-8").rstrip("\n")
    path.write_bytes((row + "\r\n").encode("utf-8"))
    assert validate_topic(topic_dir) == []

    path.write_text(row + "\r" + row + "\n", "utf-8")
    errors = validate_topic(topic_dir)
    assert any("sources.jsonl:1" in e for e in errors), errors


def test_corrupt_line_keeps_valid_rows_and_reports_all_damage(topic_dir):
    # One damaged row does not discard the file. A valid source remains known, preventing a cascade
    # of false foreign-key errors, and every damaged row is reported.
    src = _src(topic_dir)
    add_claim(topic_dir, "staging", **_claim_kw(src, status="candidate"))
    add_claim(topic_dir, "final", **_claim_kw(src, text="final fact"))
    p = topic_dir / "sources" / "sources.jsonl"
    p.write_text(p.read_text("utf-8") + "{broken\n[1, 2]\n", "utf-8")
    errors = validate_topic(topic_dir)
    assert any("sources.jsonl:2" in e for e in errors), errors
    assert any("sources.jsonl:3" in e for e in errors), errors
    assert not any("FK" in e for e in errors), errors


def test_corrupt_final_line_does_not_forge_wiki_fk(topic_dir):
    # Valid final claims remain in final_ids. Otherwise one bad claims.jsonl row would make every
    # live wiki page appear to reference a missing claim.
    src = _src(topic_dir)
    cid = add_claim(topic_dir, "final", **_claim_kw(src))
    add_wiki_page(topic_dir, title="Concept", claims=[cid], body="body")
    p = topic_dir / "final" / "claims.jsonl"
    p.write_text(p.read_text("utf-8") + "{broken\n", "utf-8")
    errors = validate_topic(topic_dir)
    assert any("claims.jsonl:2" in e for e in errors), errors
    assert not any("absent from final/claims.jsonl" in e for e in errors), errors


@pytest.mark.parametrize("rel", ["sources/sources.jsonl", "staging/claims.jsonl",
                                 "final/claims.jsonl", "final/wiki/concept.md", "topic.json"])
def test_unreadable_file_is_a_finding_not_a_crash(topic_dir, rel):
    # validate_topic promises list[str] for every input. Invalid UTF-8 was once read outside
    # CorruptLine handling and leaked a UnicodeDecodeError traceback to the consumer.
    src = _src(topic_dir)
    add_claim(topic_dir, "staging", **_claim_kw(src, status="candidate"))
    cid = add_claim(topic_dir, "final", **_claim_kw(src, text="final fact"))
    add_wiki_page(topic_dir, title="Concept", claims=[cid], body="body")
    (topic_dir / rel).write_bytes(b"\xff\xfe not utf-8\n")
    errors = validate_topic(topic_dir)  # Verify a return value, not an exception.
    assert errors and all(isinstance(e, str) for e in errors), errors
    assert any(Path(rel).name in e for e in errors), errors


def test_jsonl_io_oserror_is_a_finding_and_write_refuses(topic_dir, monkeypatch):
    src = _src(topic_dir)
    path = topic_dir / "sources" / "sources.jsonl"
    real_read_bytes = Path.read_bytes

    def fail_target_read(candidate):
        if candidate == path:
            raise OSError("registry read failure")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", fail_target_read)
    errors = validate_topic(topic_dir)
    assert any("sources.jsonl" in e and "OSError" in e for e in errors), errors
    with pytest.raises(CorruptLine, match="sources.jsonl"):
        add_claim(topic_dir, "final", **_claim_kw(src))


def test_wiki_traversal_oserror_is_a_finding_not_a_crash(topic_dir, monkeypatch):
    def fail_scandir(_path):
        raise OSError("wiki traversal failure")

    monkeypatch.setattr("llmwiki.store.os.scandir", fail_scandir)
    errors = validate_topic(topic_dir)
    assert errors and all(isinstance(e, str) for e in errors), errors
    assert any("final/wiki" in e and "OSError" in e and "wiki traversal failure" in e
               for e in errors), errors


def test_write_refuses_on_unreadable_registry(topic_dir):
    # A write against an unreadable registry fails instead of silently losing every source from FK checks.
    src = _src(topic_dir)
    (topic_dir / "sources" / "sources.jsonl").write_bytes(b"\xff\xfe\n")
    with pytest.raises(CorruptLine, match="sources.jsonl"):
        add_claim(topic_dir, "final", **_claim_kw(src))


def test_write_refuses_on_corrupt_registry(topic_dir):
    # A write into a topic with a broken registry fails closed with filename and line number instead
    # of silently skipping the unreadable row and corrupting foreign-key and deduplication results.
    src = _src(topic_dir)
    p = topic_dir / "sources" / "sources.jsonl"
    p.write_text(p.read_text("utf-8") + "{broken\n", "utf-8")
    with pytest.raises(CorruptLine, match="sources.jsonl:2"):
        add_claim(topic_dir, "final", **_claim_kw(src))


def test_spec_fail_closed():
    load_schema("claim")  # The engine understands the current schemas completely.
    import llmwiki.validate as v
    bad = {"type": "object", "if": {}}
    with pytest.raises(SpecError):
        v._check_known(bad, "x")


def test_cli_smoke(tmp_path):
    env_dir = Path(__file__).parent.parent
    r = subprocess.run([sys.executable, "-m", "llmwiki", "init", str(tmp_path), "Topic X"],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0, r.stderr
    tdir = r.stdout.strip()
    src = json.dumps({"kind": "web", "title": "t", "url": "https://e.com"})
    r = subprocess.run([sys.executable, "-m", "llmwiki", "add-source", tdir],
                       input=src, capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "-m", "llmwiki", "validate", tdir],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0 and "OK" in r.stdout
