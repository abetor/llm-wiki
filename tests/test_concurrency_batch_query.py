"""Regressions from a 2026-08-19/20 production course run over six topics and 389 failures.

Three finding classes each have a dedicated test:
1. A mutator race where shared `<file>.tmp` without locking silently lost a record. Six parallel
   workers returned a valid claim id that never reached the file.
2. Missing batch operations made 276 claims rewrite an entire zone file 276 times.
3. Missing read and audit operations allowed `validate OK` on an incomplete corpus.
"""
import json
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import add_claim, add_source, init_topic  # noqa: E402
from llmwiki.cli import EXIT_FAIL, EXIT_OK, main  # noqa: E402
from llmwiki.ext import query  # noqa: E402
from llmwiki.store import (add_claims, promote_claims, put_wiki_page,  # noqa: E402
                           retract_claim, wiki_page_path)


@pytest.fixture
def topic(tmp_path):
    root = init_topic(tmp_path, "Topic about agents", slug="agents")
    src = add_source(root, kind="local", title="Lesson 001",
                     content_path="sources/content/001.md")
    (root / "sources" / "content").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "content" / "001.md").write_text("word " * 1000, "utf-8")
    return root, src


def _claim(src, text, status="verified"):
    return {"text": text, "confidence": "high", "status": status,
            "evidence": [{"source_id": src, "quote": "quote", "stance": "supports"}],
            "tags": ["agents"]}


# --- 1. race ---

def _writer(args):
    """Use a separate process, not a thread, because only processes verify an interprocess lock."""
    root, src, i = args
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from llmwiki import add_claim as ac
    from llmwiki import add_source as as_
    as_(root, kind="local", title="Lesson 001", content_path="sources/content/001.md")
    ac(root, "staging", text=f"Claim number {i}", confidence="high",
       status="verified",
       evidence=[{"source_id": src, "quote": "quote", "stance": "supports"}])
    return i


def test_parallel_add_claim_ne_teryaet_zapisi(topic):
    """Concurrent add-claim calls once lost a row silently.

    A successful return must mean the row exists in the file. Before locking and unique temporary
    paths, records disappeared when a neighboring writer replaced the shared `.tmp` file.
    """
    root, src = topic
    n = 12
    with mp.get_context("spawn").Pool(4) as pool:
        got = pool.map(_writer, [(str(root), src, i) for i in range(n)])
    assert sorted(got) == list(range(n))
    rows = query.read_claims(root, "staging")
    texts = {r["text"] for r in rows}
    assert len(rows) == n, f"lost records: {n - len(rows)}"
    assert texts == {f"Claim number {i}" for i in range(n)}


def test_tmp_file_unikalen_na_process(topic):
    import os

    from llmwiki import store
    root, _ = topic
    path = root / "staging" / "claims.jsonl"
    a, b = store._tmp_path(path), store._tmp_path(path)
    assert a != b, "two calls returned the same temporary name, recreating the race"
    assert str(os.getpid()) in a.name


# --- 2. batch operations ---

def test_add_claims_partiya_i_dubli(topic):
    root, src = topic
    batch = [_claim(src, "First"), _claim(src, "Second"), _claim(src, "First")]
    out = add_claims(root, "staging", batch)
    assert [o["result"] for o in out] == ["written", "written", "duplicate"]
    assert len(query.read_claims(root, "staging")) == 2
    # Repeating the complete batch makes every item a duplicate without growing the file.
    again = add_claims(root, "staging", batch)
    assert {o["result"] for o in again} == {"duplicate"}
    assert len(query.read_claims(root, "staging")) == 2


def test_add_claims_nevalidnaya_partiya_ne_pishet_nichego(topic):
    root, src = topic
    bad = [_claim(src, "Good"), {**_claim(src, "Bad"), "confidence": "unknown"}]
    with pytest.raises(Exception):
        add_claims(root, "staging", bad)
    assert query.read_claims(root, "staging") == []


def test_promote_claims_partiey(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(5)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    out = promote_claims(root, ids)
    assert {o["result"] for o in out} == {"promoted"}
    assert len(query.read_claims(root, "final")) == 5
    assert query.read_claims(root, "staging") == []
    # Repeating the same ids fails before writing because they are no longer in staging.
    with pytest.raises(KeyError):
        promote_claims(root, ids)


def test_promote_claims_otkaz_na_ne_verified_ne_trogaet_zonu(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, "Ready"),
                                 _claim(src, "Raw", status="candidate")])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    with pytest.raises(Exception):
        promote_claims(root, ids)
    assert query.read_claims(root, "final") == []
    assert len(query.read_claims(root, "staging")) == 2


# --- 3. wiki: slug, upsert, and lookup by id ---

def test_put_wiki_upsert_i_yavnyy_slug(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, "Page claim")])
    cid = query.read_claims(root, "staging")[0]["id"]
    promote_claims(root, [cid])
    first = put_wiki_page(root, title="Agent memory", claims=[cid],
                          body=f"Text ({cid}).", slug="agent-memory")
    assert first["result"] == "created"
    assert Path(first["path"]).name == "agent-memory.md"
    second = put_wiki_page(root, title="Agent memory", claims=[cid],
                           body=f"Different text ({cid}).", slug="agent-memory")
    assert second["result"] == "updated"
    assert second["path"] == first["path"]
    assert len(list((root / "final" / "wiki").glob("*.md"))) == 1


def test_pravka_zagolovka_ne_plodit_vtoruyu_strancu(topic):
    """Page ids use normalized titles while filenames use raw titles.

    Changing title case or punctuation once created a second file with the same id.
    """
    root, src = topic
    add_claims(root, "staging", [_claim(src, "Page claim")])
    cid = query.read_claims(root, "staging")[0]["id"]
    promote_claims(root, [cid])
    put_wiki_page(root, title="Agent memory", claims=[cid], body=f"Text ({cid}).")
    put_wiki_page(root, title="AGENT  memory", claims=[cid], body=f"Edit ({cid}).")
    pages = list((root / "final" / "wiki").glob("*.md"))
    assert len(pages) == 1, [p.name for p in pages]
    assert wiki_page_path(root, "AGENT  memory") == pages[0]


def test_lock_fajly_ne_popadayut_v_obhod_stranic(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, "Page claim")])
    cid = query.read_claims(root, "staging")[0]["id"]
    promote_claims(root, [cid])
    put_wiki_page(root, title="Page", claims=[cid], body=f"Text ({cid}).")
    assert [p["slug"] for p in query.read_pages(root)] == ["page"]
    assert any(p.name.startswith(".") for p in (root / "final" / "wiki").iterdir())


# --- 4. reads, funnel, and audit ---

def test_list_claims_polya_i_filtry(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, "About memory"), _claim(src, "About tools")])
    only_ids = query.list_claims(root, fields=["id"])
    assert all(set(r) == {"id"} for r in only_ids)
    assert len(query.list_claims(root, contains="memory")) == 1
    assert len(query.list_claims(root, tag="agents")) == 2
    assert len(query.list_claims(root, tag="unknown")) == 0
    assert len(query.list_claims(root, source=src)) == 2
    assert len(query.list_claims(root, limit=1)) == 1
    assert "evidence" in query.list_claims(root, fields=["*"])[0]


def test_stats_voronka(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(4)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    body = "word " * 400 + " ".join(f"({i})" for i in ids)
    put_wiki_page(root, title="Page", claims=ids, body=body)
    st = query.stats(root)
    assert st["sources"] == 1 and st["sources_words"] == 1000
    assert st["claims_final"] == 4 and st["claims_staging"] == 0
    assert st["pages"] == 1 and st["final_claims_orphan"] == 0
    assert st["words_per_claim"] > 60


def test_audit_lovit_sirot_i_skelet(topic):
    """Reproduce the case where validation passed an intact format with unfinished work."""
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(4)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    put_wiki_page(root, title="Page", claims=ids[:2],
                  body=f"Very short ({ids[0]}) ({ids[1]}).")
    codes = {f["code"] for f in query.audit(root)}
    assert "claims-orphan" in codes
    assert "page-too-thin" in codes
    assert "staging-not-empty" not in codes


def test_audit_lovit_klejm_bez_yakorya(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(2)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    put_wiki_page(root, title="Page", claims=ids, body="word " * 300 + f"({ids[0]})")
    found = [f for f in query.audit(root) if f["code"] == "claims-without-anchor"]
    assert found and found[0]["claims"] == [ids[1]]
    assert not [f for f in query.audit(root, require_anchors=False)
                if f["code"] == "claims-without-anchor"]


def test_audit_chisto_na_zdorovoj_teme(topic):
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(3)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    put_wiki_page(root, title="Page", claims=ids,
                  body="word " * 400 + " ".join(f"({i})" for i in ids))
    assert query.audit(root) == []


# --- 4b. retracted rows are not live knowledge, following ASR-loop cleanup ---

def test_stats_schitaet_tolko_zhivoe_znanie(topic):
    """A dead row provides no knowledge, so including it in wiki density distorts the funnel."""
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(4)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    put_wiki_page(root, title="Page", claims=ids,
                  body="word " * 400 + " ".join(f"({i})" for i in ids))
    before = query.stats(root)
    assert (before["claims_final"], before["claims_retracted"]) == (4, 0)

    retract_claim(root, "final", ids[3])
    st = query.stats(root)
    assert st["claims_final"] == 3, "retracted row counted as a live final claim"
    assert st["claims_retracted"] == 1
    assert st["words_per_claim"] == round(st["wiki_words"] / 3, 1)
    assert st["claims_per_source"] == 3.0
    assert st["final_claims_on_pages"] == 3
    assert st["final_claims_orphan"] == 0

    # A staging tombstone is also not knowledge. Unlike the old behavior it remains visible in the
    # funnel because claims_retracted counts both zones.
    add_claims(root, "staging", [_claim(src, "Raw")])
    dead_staging = [c["id"] for c in query.read_claims(root, "staging")][0]
    retract_claim(root, "staging", dead_staging)
    st = query.stats(root)
    assert st["claims_staging"] == 0
    assert st["claims_retracted"] == 2


def test_audit_ne_schitaet_retracted_staging_nerazobrannym(topic):
    """Reproduce a dead end where every staging row is a tombstone and warning cannot be resolved."""
    root, src = topic
    add_claims(root, "staging", [_claim(src, "Retracted")])
    dead = [c["id"] for c in query.read_claims(root, "staging")][0]
    assert [f["code"] for f in query.audit(root) if f["code"] == "staging-not-empty"]

    retract_claim(root, "staging", dead)
    assert not [f for f in query.audit(root) if f["code"] == "staging-not-empty"]

    # The gate still reports a live unprocessed row next to the tombstone and counts only live rows.
    add_claims(root, "staging", [_claim(src, "Live but unprocessed")])
    found = [f for f in query.audit(root) if f["code"] == "staging-not-empty"]
    assert found and found[0]["count"] == 1


def test_audit_istochnik_tolko_s_retracted_snova_molchit(topic):
    """A source whose claims were retracted produced no knowledge, and the audit must show it."""
    root, src = topic
    other = add_source(root, kind="local", title="Lesson 002",
                       content_path="sources/content/002.md")
    (root / "sources" / "content" / "002.md").write_text("word " * 10, "utf-8")
    add_claims(root, "staging", [_claim(src, "Live claim"), _claim(other, "Retracted claim")])
    ids = {c["text"]: c["id"] for c in query.read_claims(root, "staging")}
    promote_claims(root, list(ids.values()))
    silent = [f for f in query.audit(root) if f["code"] == "sources-without-claims"]
    assert not silent, "both sources produced claims, so neither is silent"

    retract_claim(root, "final", ids["Retracted claim"])
    silent = [f for f in query.audit(root) if f["code"] == "sources-without-claims"]
    assert silent and silent[0]["sources"] == [other] and silent[0]["count"] == 1


def test_audit_lovit_stranicu_na_retracted_klejme(topic):
    """This is invalid finished knowledge, not a metric: a page depends on a withdrawn fact."""
    root, src = topic
    add_claims(root, "staging", [_claim(src, f"Claim {i}") for i in range(3)])
    ids = [c["id"] for c in query.read_claims(root, "staging")]
    promote_claims(root, ids)
    put_wiki_page(root, title="Page", claims=ids,
                  body="word " * 400 + f"({ids[0]}) ({ids[1]})")
    before = [f for f in query.audit(root) if f["code"] == "claims-without-anchor"]
    assert before and before[0]["claims"] == [ids[2]]

    retract_claim(root, "final", ids[2])
    found = query.audit(root)
    dead = [f for f in found if f["code"] == "page-claims-retracted"]
    assert dead and dead[0]["level"] == "error"
    assert dead[0]["page"] == "page" and dead[0]["claims"] == [ids[2]]
    # One claim produces one finding. The dead reference is already explained, so do not repeat it.
    assert not [f for f in found if f["code"] == "claims-without-anchor"]
    assert not [f for f in found if f["code"] == "claims-orphan"]
    assert _run(["audit", str(root)]).returncode == EXIT_FAIL

    # It is also excluded from claims assigned to multiple pages.
    put_wiki_page(root, title="Second", claims=[ids[0], ids[2]],
                  body="word " * 400 + f"({ids[0]})")
    multi = [f for f in query.audit(root) if f["code"] == "claims-on-many-pages"]
    assert multi and list(multi[0]["claims"]) == [ids[0]]


# --- 5. CLI ---

def _run(args, stdin=""):
    return subprocess.run([sys.executable, "-m", "llmwiki", *args], input=stdin,
                          capture_output=True, text=True,
                          cwd=str(Path(__file__).parent.parent))


def test_cli_doctor_prinimaet_put_temy(topic):
    root, _ = topic
    r = _run(["doctor", str(root)])
    assert r.returncode == EXIT_OK, r.stderr
    assert "slug=agents" in r.stdout


def test_cli_doctor_ne_tema_eto_otkaz(tmp_path):
    r = _run(["doctor", str(tmp_path)])
    assert r.returncode == EXIT_FAIL
    assert "NOT READY" in r.stdout


def test_cli_batch_i_audit(topic):
    root, src = topic
    batch = json.dumps([_claim(src, f"Claim {i}") for i in range(3)], ensure_ascii=False)
    r = _run(["add-claim", str(root), "staging", "--batch"], stdin=batch)
    assert r.returncode == EXIT_OK, r.stderr
    ids = [o["id"] for o in json.loads(r.stdout)]
    r = _run(["promote", str(root), "--batch"], stdin="\n".join(ids))
    assert r.returncode == EXIT_OK, r.stderr
    assert {o["result"] for o in json.loads(r.stdout)} == {"promoted"}
    page = json.dumps({"title": "Page", "claims": ids, "slug": "page",
                       "body": "word " * 400 + " ".join(f"({i})" for i in ids)},
                      ensure_ascii=False)
    r = _run(["put-wiki", str(root)], stdin=page)
    assert r.returncode == EXIT_OK, r.stderr
    assert json.loads(r.stdout)["result"] == "created"
    r = _run(["audit", str(root)])
    assert r.returncode == EXIT_OK, r.stdout
    assert json.loads(r.stdout) == []
    r = _run(["stats", str(root)])
    assert json.loads(r.stdout)["claims_final"] == 3


def test_cli_audit_vozvrashaet_1_na_error(topic):
    root, src = topic
    add_claim(root, "final", **_claim(src, "Unassigned claim"))
    r = _run(["audit", str(root)])
    assert r.returncode == EXIT_FAIL
    assert any(f["code"] == "claims-orphan" for f in json.loads(r.stdout))


def test_cli_promote_bez_id_i_bez_batch_eto_oshibka(topic):
    root, _ = topic
    r = _run(["promote", str(root)])
    assert r.returncode == EXIT_FAIL
    assert "claim_id" in r.stderr


def test_main_v_processe_tozhe_rabotaet(topic, capsys):
    root, _ = topic
    assert main(["stats", str(root)]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["slug"] == "agents"
