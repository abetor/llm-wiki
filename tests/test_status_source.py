"""Two operations missing from a corpus assembled outside the course pipeline on 2026-08-20.

They transition candidate to verified and add content_path to an existing source. In production,
1,571 imported claims were written in batches as candidate while promote accepted only verified.
At the same time verify-quotes was blind without content_path, so all 618 claims in one topic
returned source-unreadable.
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import (add_claim, add_source, init_topic, merge_claim_evidence,  # noqa: E402
                     promote_claim, read_sources, retract_claim, set_claim_status,
                     update_source, validate_topic)
from llmwiki import cli as wiki_cli  # noqa: E402
from llmwiki.ext import evidence as evidence_ext  # noqa: E402
from llmwiki.validate import ValidationError  # noqa: E402


@pytest.fixture
def topic_dir(tmp_path):
    return init_topic(tmp_path, "Status and source ops")


def _src(topic_dir, url="https://example.com/a"):
    return add_source(topic_dir, kind="web", title="Article", url=url)


def _claim(topic_dir, src, text="Corpus fact", status="candidate", quote="verbatim quote"):
    return add_claim(topic_dir, "staging", text=text, confidence="medium", status=status,
                     evidence=[{"source_id": src, "quote": quote, "stance": "supports"}])


def _rows(topic_dir, zone="staging"):
    path = Path(topic_dir) / zone / "claims.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# --- set_claim_status ------------------------------------------------------

def test_candidate_stanovitsya_verified_i_promoutitsya(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    with pytest.raises(ValidationError):
        promote_claim(topic_dir, cid)  # Promotion is forbidden before the transition.

    assert set_claim_status(topic_dir, [cid]) == [{"id": cid, "result": "verified"}]
    assert _rows(topic_dir)[0]["status"] == "verified"
    assert promote_claim(topic_dir, cid) == cid
    assert validate_topic(topic_dir) == []


def test_povtor_partii_idempotenten(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    set_claim_status(topic_dir, [cid])
    assert set_claim_status(topic_dir, [cid]) == [{"id": cid, "result": "already-verified"}]


def test_retracted_ne_voskreshaetsya(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    retract_claim(topic_dir, "staging", cid)
    with pytest.raises(ValidationError):
        set_claim_status(topic_dir, [cid])
    assert _rows(topic_dir)[0]["status"] == "retracted"


def test_partiya_celikom_ili_nikak(topic_dir):
    src = _src(topic_dir)
    good = _claim(topic_dir, src, text="First fact")
    with pytest.raises(KeyError):
        set_claim_status(topic_dir, [good, "clm_deadbeef0000"])
    # No row changed because a partially applied batch is indistinguishable from interruption.
    assert _rows(topic_dir)[0]["status"] == "candidate"


def test_chuzhoy_status_otvergaetsya(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    with pytest.raises(ValueError):
        set_claim_status(topic_dir, [cid], "final")


# --- update_source ---------------------------------------------------------

def test_content_path_dopisyvaetsya_sushestvuyushemu(topic_dir, tmp_path):
    src = _src(topic_dir)
    assert "content_path" not in read_sources(topic_dir)[0]
    text = tmp_path / "transkript.txt"
    text.write_text("a verbatim quote inside the text", "utf-8")

    row = update_source(topic_dir, src, content_path=str(text))
    assert row["content_path"] == str(text)
    saved = read_sources(topic_dir)[0]
    assert saved["content_path"] == str(text)
    assert saved["id"] == src and saved["url"] == "https://example.com/a"
    assert validate_topic(topic_dir) == []


def test_otnositelnyy_put_schitaetsya_ot_temy(topic_dir):
    src = _src(topic_dir)
    inner = Path(topic_dir) / "sources" / "content"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "001.md").write_text("text", "utf-8")
    update_source(topic_dir, src, content_path="sources/content/001.md")
    assert read_sources(topic_dir)[0]["content_path"] == "sources/content/001.md"


def test_put_v_nikuda_otvergaetsya(topic_dir):
    src = _src(topic_dir)
    with pytest.raises(ValueError):
        update_source(topic_dir, src, content_path="/net/takogo/faila.txt")
    assert "content_path" not in read_sources(topic_dir)[0]


def test_neizvestnyy_istochnik(topic_dir, tmp_path):
    text = tmp_path / "t.txt"
    text.write_text("x", "utf-8")
    with pytest.raises(KeyError):
        update_source(topic_dir, "src_000000000000", content_path=str(text))


def test_pustoe_obnovlenie_otvergaetsya(topic_dir):
    with pytest.raises(ValueError):
        update_source(topic_dir, _src(topic_dir))


def test_title_bez_url_ne_menyaet_deterministic_identity(topic_dir):
    src = add_source(topic_dir, kind="local", title="Old name")
    before = (Path(topic_dir) / "sources" / "sources.jsonl").read_bytes()

    result = _cli(topic_dir, "update-source", str(topic_dir), src,
                  "--title", "New name")

    assert result.returncode == 1
    assert "deterministic identity" in result.stderr
    assert (Path(topic_dir) / "sources" / "sources.jsonl").read_bytes() == before
    saved = read_sources(topic_dir)
    assert len(saved) == 1
    assert saved[0]["id"] == src and saved[0]["title"] == "Old name"


def test_title_s_url_mozhno_obnovit_identity_po_url_stabilna(topic_dir):
    src = _src(topic_dir)
    row = update_source(topic_dir, src, title="New name")
    assert row["id"] == src and row["title"] == "New name"
    assert add_source(topic_dir, kind="web", title="Another name",
                      url="https://example.com/a") == src
    assert len(read_sources(topic_dir)) == 1


# --- CLI and quote gate ----------------------------------------------------

def _cli(topic_dir, *args, stdin=""):
    return subprocess.run([sys.executable, "-m", "llmwiki", *args],
                          input=stdin, capture_output=True, text=True,
                          cwd=str(Path(__file__).parent.parent))


def test_cli_geyt_citat_derzhit_perevod_bez_content_path(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    r = _cli(topic_dir, "set-status", str(topic_dir), cid)
    assert r.returncode == 1
    assert "quote gate failed" in r.stderr
    assert _rows(topic_dir)[0]["status"] == "candidate"


def test_cli_perevod_prohodit_kogda_citata_naydena(topic_dir, tmp_path):
    src = _src(topic_dir)
    cid = _claim(topic_dir, src, quote="verbatim quote")
    text = tmp_path / "t.txt"
    text.write_text("surrounding words and a verbatim quote inside the text", "utf-8")
    assert _cli(topic_dir, "update-source", str(topic_dir), src,
                "--content-path", str(text)).returncode == 0

    r = _cli(topic_dir, "set-status", str(topic_dir), cid)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [{"id": cid, "result": "verified"}]


def test_cli_vydumannaya_citata_ne_prohodit_geyt(topic_dir, tmp_path):
    src = _src(topic_dir)
    cid = _claim(topic_dir, src, quote="this phrase is absent from the source")
    text = tmp_path / "t.txt"
    text.write_text("completely different source text", "utf-8")
    _cli(topic_dir, "update-source", str(topic_dir), src, "--content-path", str(text))

    r = _cli(topic_dir, "set-status", str(topic_dir), cid)
    assert r.returncode == 1
    assert cid in r.stderr
    assert _rows(topic_dir)[0]["status"] == "candidate"


def test_cli_skip_quote_check_yavnyy_obhod(topic_dir):
    cid = _claim(topic_dir, _src(topic_dir))
    r = _cli(topic_dir, "set-status", str(topic_dir), cid, "--skip-quote-check")
    assert r.returncode == 0, r.stderr
    assert _rows(topic_dir)[0]["status"] == "verified"


def test_cli_batch_perevodit_partiyu(topic_dir, tmp_path):
    src = _src(topic_dir)
    ids = [_claim(topic_dir, src, text=f"Fact number {i}") for i in range(3)]
    r = _cli(topic_dir, "set-status", str(topic_dir), "--batch", "--skip-quote-check",
             stdin=json.dumps(ids))
    assert r.returncode == 0, r.stderr
    assert [x["result"] for x in json.loads(r.stdout)] == ["verified"] * 3
    assert {row["status"] for row in _rows(topic_dir)} == {"verified"}


def test_cli_geyt_smotrit_tolko_svoyu_partiyu(topic_dir, tmp_path):
    """Unrelated findings in a topic must not block transitions for verified claims."""
    src_ok = _src(topic_dir, url="https://example.com/ok")
    text = tmp_path / "ok.txt"
    text.write_text("this contains a complete good quote", "utf-8")
    good = _claim(topic_dir, src_ok, text="Good fact", quote="good quote")
    _cli(topic_dir, "update-source", str(topic_dir), src_ok, "--content-path", str(text))
    _claim(topic_dir, _src(topic_dir, url="https://example.com/bad"), text="Broken fact")

    r = _cli(topic_dir, "set-status", str(topic_dir), good)
    assert r.returncode == 0, r.stderr
    statuses = {row["id"]: row["status"] for row in _rows(topic_dir)}
    assert statuses[good] == "verified"


def test_cli_quote_gate_i_status_write_pod_odnim_lokom(topic_dir, tmp_path, monkeypatch):
    """Merge cannot interleave between a successful quote check and the verified write."""
    src_a = _src(topic_dir, url="https://example.com/readable")
    text = tmp_path / "readable.txt"
    text.write_text("a verbatim quote is stored here", "utf-8")
    update_source(topic_dir, src_a, content_path=str(text))
    cid = _claim(topic_dir, src_a)
    src_b = _src(topic_dir, url="https://example.com/unreadable")
    unchecked = {"source_id": src_b, "quote": "unchecked quote", "stance": "supports"}

    merge_started = threading.Event()
    merge_finished = threading.Event()
    merge_thread = None
    finished_during_gate = []
    merge_errors = []
    real_verify_quotes = evidence_ext.verify_quotes

    def merge_unchecked():
        merge_started.set()
        try:
            merge_claim_evidence(topic_dir, "staging", cid, [unchecked])
        except Exception as exc:
            merge_errors.append(exc)
        finally:
            merge_finished.set()

    def controlled_verify_quotes(*args, **kwargs):
        nonlocal merge_thread
        findings = real_verify_quotes(*args, **kwargs)
        assert not [f for f in findings if f["level"] == "error"]
        merge_thread = threading.Thread(target=merge_unchecked)
        merge_thread.start()
        assert merge_started.wait(1)
        finished_during_gate.append(merge_finished.wait(0.2))
        return findings

    monkeypatch.setattr(evidence_ext, "verify_quotes", controlled_verify_quotes)
    assert wiki_cli.main(["set-status", str(topic_dir), cid]) == 0
    merge_thread.join(1)

    assert not merge_thread.is_alive()
    assert merge_errors == []
    assert finished_during_gate == [False]
    assert _rows(topic_dir)[0]["status"] == "verified"
    assert len(_rows(topic_dir)[0]["evidence"]) == 2
