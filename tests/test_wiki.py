import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import (add_claim, add_source, add_wiki_page, delete_wiki_page,  # noqa: E402
                     init_topic, make_wiki_id, parse_page, put_wiki_page,
                     render_page, update_wiki_page, validate_topic, wiki_page_path)
from llmwiki.frontmatter import (FrontmatterError, dump_frontmatter,  # noqa: E402
                                 parse_frontmatter)
from llmwiki.validate import ValidationError, load_schema  # noqa: E402


@pytest.fixture
def topic_dir(tmp_path):
    return init_topic(tmp_path, "Topic about RAG")


def _final_claim(topic_dir, text="Vector search returns top-k"):
    src = add_source(topic_dir, kind="web", title="Article", url="https://example.com/a")
    return add_claim(topic_dir, "final", text=text, confidence="high", status="verified",
                     evidence=[{"source_id": src, "quote": "top-k", "stance": "supports"}])


# --- frontmatter codec: round-trip and fail-closed behavior ---

def test_frontmatter_roundtrip_tricky():
    # Exercise internal quotes and a colon; the next test covers a leading quote separately.
    data = {"id": "wik_0123456789ab", "title": 'The "N+1" problem: an overview',
            "claims": ["clm_aaaaaaaaaaaa", "clm_bbbbbbbbbbbb"],
            "updated_at": "2026-07-06T00:00:00+00:00"}
    body = "Page body.\nA second line with text."
    got_data, got_body = parse_page(render_page(data, body))
    assert got_data == data
    assert got_body == body


def test_frontmatter_leading_quote_and_spaces_survive():
    # Values unsafe for plain representation must be encoded with JSON quotes.
    data = {"title": '"Quote" as a term'}
    assert parse_frontmatter(dump_frontmatter(data)) == data
    data2 = {"title": "  edge whitespace  "}
    assert parse_frontmatter(dump_frontmatter(data2)) == data2


def test_frontmatter_rejects_garbage():
    with pytest.raises(FrontmatterError):
        parse_page("no frontmatter at all")
    with pytest.raises(FrontmatterError):
        parse_page("---\nid: wik_x\n")  # No closing delimiter.
    with pytest.raises(FrontmatterError):
        parse_frontmatter("  - orphan item without a key\n")
    with pytest.raises(FrontmatterError):
        parse_frontmatter('title: "unclosed quote\n')
    with pytest.raises(FrontmatterError):
        parse_frontmatter("invalid line without a colon\n")


def test_no_type_coercion():
    # The subset contains only strings, so '123' and 'true' stay strings rather than typed values.
    data = parse_frontmatter("a: 123\nb: true\n")
    assert data == {"a": "123", "b": "true"}


# --- id ---

def test_wiki_id_deterministic():
    a = make_wiki_id("rag", "Vector databases")
    assert a == make_wiki_id("rag", "  vector   databases ")  # Text normalization.
    assert a != make_wiki_id("rag", "Reranking")
    assert a.startswith("wik_") and len(a) == 16


def test_wiki_schema_understood_by_engine():
    load_schema("wiki")  # An unsupported keyword raises SpecError here.


# --- add / update ---

def test_add_wiki_page_writes_valid_file(topic_dir):
    cid = _final_claim(topic_dir)
    wid = add_wiki_page(topic_dir, title="Vector search", claims=[cid],
                        body="# Vector search\n\nThe concept explained.")
    path = topic_dir / "final" / "wiki" / "vector-search.md"
    assert path.exists()
    assert wid == make_wiki_id("topic-about-rag", "Vector search")
    page, body = parse_page(path.read_text("utf-8"))
    assert page["id"] == wid and page["title"] == "Vector search"
    assert page["claims"] == [cid]
    assert "The concept explained." in body
    assert validate_topic(topic_dir) == []


def test_add_wiki_duplicate_is_error(topic_dir):
    cid = _final_claim(topic_dir)
    add_wiki_page(topic_dir, title="Reranking", claims=[cid])
    with pytest.raises(FileExistsError):
        add_wiki_page(topic_dir, title="Reranking", claims=[cid])


def test_update_rewrites_same_file(topic_dir):
    cid = _final_claim(topic_dir)
    cid2 = _final_claim(topic_dir, text="A reranker refines ordering")
    wid = add_wiki_page(topic_dir, title="Pipeline", claims=[cid], body="v1")
    path = topic_dir / "final" / "wiki" / "pipeline.md"
    wid2 = update_wiki_page(topic_dir, title="Pipeline", claims=[cid, cid2], body="v2")
    assert wid == wid2  # The same id resolves to the same page.
    assert len(list((topic_dir / "final" / "wiki").glob("*.md"))) == 1
    page, body = parse_page(path.read_text("utf-8"))
    assert page["claims"] == [cid, cid2] and body == "v2"


def test_update_missing_is_error(topic_dir):
    cid = _final_claim(topic_dir)
    with pytest.raises(FileNotFoundError):
        update_wiki_page(topic_dir, title="Missing page", claims=[cid])


def test_cyrillic_title_makes_readable_slug(topic_dir):
    cid = _final_claim(topic_dir)
    add_wiki_page(topic_dir, title="Гибридный поиск BM25 и векторы", claims=[cid])
    assert (topic_dir / "final" / "wiki" / "гибридный-поиск-bm25-и-векторы.md").exists()


# --- delete ---

def test_delete_wiki_page_existing_then_missing(topic_dir):
    cid = _final_claim(topic_dir)
    created = put_wiki_page(topic_dir, title="Old page", claims=[cid],
                            slug="staraya-stranica")
    path = Path(created["path"])
    claims_before = (topic_dir / "final" / "claims.jsonl").read_bytes()

    assert delete_wiki_page(topic_dir, slug="staraya-stranica") == {
        "path": str(path), "result": "deleted"}
    assert not path.exists()
    assert delete_wiki_page(topic_dir, slug="staraya-stranica") == {
        "path": str(path), "result": "missing"}
    assert (topic_dir / "final" / "claims.jsonl").read_bytes() == claims_before


@pytest.mark.parametrize("slug", ["", "page.md", "nested/page"])
def test_delete_wiki_page_rejects_bad_slug(topic_dir, slug):
    with pytest.raises(ValueError):
        delete_wiki_page(topic_dir, slug=slug)
    with pytest.raises(ValueError):
        wiki_page_path(topic_dir, "Title is irrelevant", slug=slug)


def test_delete_wiki_page_does_not_follow_symlink_outside_wiki(topic_dir, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("not a topic page", "utf-8")
    link = topic_dir / "final" / "wiki" / "outside-link.md"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="outside final/wiki"):
        delete_wiki_page(topic_dir, slug="outside-link")

    assert outside.read_text("utf-8") == "not a topic page"
    assert link.is_symlink()


def test_delete_resolves_duplicate_wiki_id_after_page_map_merge(topic_dir):
    cid = _final_claim(topic_dir)
    first = put_wiki_page(topic_dir, title="Keys in a short description", claims=[cid],
                          slug="101-chastota-klyuchei-v-kratkom-opisanii")
    second = put_wiki_page(topic_dir, title="Keys in a short description", claims=[cid],
                           slug="131-klyuchi-v-kratkom-opisanii")
    assert first["id"] == second["id"]

    errors = validate_topic(topic_dir)
    assert any("131-klyuchi-v-kratkom-opisanii.md" in error and "duplicate id" in error
               for error in errors), errors

    delete_wiki_page(topic_dir, slug="131-klyuchi-v-kratkom-opisanii")
    assert validate_topic(topic_dir) == []


# --- Foreign keys are required and must point to final ---

def test_wiki_fk_fail_unknown_claim(topic_dir):
    with pytest.raises(ValidationError, match="FK"):
        add_wiki_page(topic_dir, title="Empty", claims=["clm_000000000000"])


def test_wiki_fk_rejects_staging_only_claim(topic_dir):
    # A claim in staging but not final cannot be referenced by the wiki.
    src = add_source(topic_dir, kind="web", title="S", url="https://e.com/s")
    sid = add_claim(topic_dir, "staging", text="draft fact", confidence="low",
                    status="candidate",
                    evidence=[{"source_id": src, "quote": "q", "stance": "mentions"}])
    with pytest.raises(ValidationError, match="FK"):
        add_wiki_page(topic_dir, title="Draft", claims=[sid])


def test_wiki_schema_rejects_empty_claims(topic_dir):
    with pytest.raises(ValidationError):
        add_wiki_page(topic_dir, title="Without claims", claims=[])


# --- validate_topic catches manual page edits ---

def test_validate_catches_wiki_hand_edit(topic_dir):
    cid = _final_claim(topic_dir)
    add_wiki_page(topic_dir, title="Edit", claims=[cid])
    path = topic_dir / "final" / "wiki" / "edit.md"
    page, body = parse_page(path.read_text("utf-8"))
    page["claims"] = ["clm_deadbeef0000"]  # Manually replace it with a nonexistent claim.
    path.write_text(render_page(page, body), "utf-8")
    errors = validate_topic(topic_dir)
    assert any("FK" in e and "wiki" in e for e in errors)


def test_validate_catches_broken_frontmatter(topic_dir):
    cid = _final_claim(topic_dir)
    add_wiki_page(topic_dir, title="Broken", claims=[cid])
    path = topic_dir / "final" / "wiki" / "broken.md"
    path.write_text("no frontmatter\nplain text\n", "utf-8")
    assert validate_topic(topic_dir)


# --- CLI ---

def test_cli_wiki_smoke(tmp_path):
    env_dir = Path(__file__).parent.parent
    root = init_topic(tmp_path, "CLI Topic")
    src = add_source(root, kind="web", title="t", url="https://e.com")
    cid = add_claim(root, "final", text="CLI fact", confidence="high", status="final",
                    evidence=[{"source_id": src, "quote": "q", "stance": "supports"}])
    payload = json.dumps({"title": "CLI Concept", "claims": [cid], "body": "body"})
    r = subprocess.run([sys.executable, "-m", "llmwiki", "add-wiki", str(root)],
                       input=payload, capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().startswith("wik_")
    r = subprocess.run([sys.executable, "-m", "llmwiki", "validate", str(root)],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


def test_cli_delete_page(tmp_path):
    env_dir = Path(__file__).parent.parent
    root = init_topic(tmp_path, "CLI Delete")
    cid = _final_claim(root)
    created = put_wiki_page(root, title="Deletable", claims=[cid], slug="udalyaemaya")

    r = subprocess.run(
        [sys.executable, "-m", "llmwiki", "delete-page", str(root), "udalyaemaya"],
        capture_output=True, text=True, cwd=env_dir)

    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"path": created["path"], "result": "deleted"}
    assert not Path(created["path"]).exists()
