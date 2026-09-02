"""merge_claim_evidence grows evidence on an existing claim in place through the socket.

The operation was added after the same fact from a new source had no path into a zone:
add_claim returned DuplicateClaim, direct JSONL was forbidden, and records accumulated as
`evidence_merged_pending`.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import (add_claim, add_source, init_topic, merge_claim_evidence,  # noqa: E402
                     promote_claim, retract_claim, validate_topic)
from llmwiki.validate import ValidationError  # noqa: E402


@pytest.fixture
def topic_dir(tmp_path):
    return init_topic(tmp_path, "Merge evidence topic")


def _src(topic_dir, url):
    return add_source(topic_dir, kind="web", title="Article", url=url)


def _rows(topic_dir, zone):
    path = topic_dir / zone / "claims.jsonl"
    return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]


@pytest.fixture
def staged(topic_dir):
    src = _src(topic_dir, "https://example.com/a")
    cid = add_claim(topic_dir, "staging", text="Fact", confidence="high", status="verified",
                    evidence=[{"source_id": src, "quote": "q1", "stance": "supports"}])
    return cid, src


def test_merge_adds_new_source_in_place(topic_dir, staged):
    cid, src_a = staged
    src_b = _src(topic_dir, "https://example.com/b")
    out = merge_claim_evidence(topic_dir, "staging", cid,
                               [{"source_id": src_b, "quote": "q2", "stance": "contradicts"}])
    assert out == [{"source_id": src_b, "result": "added"}]
    rows = _rows(topic_dir, "staging")
    assert len(rows) == 1 and rows[0]["id"] == cid and rows[0]["text"] == "Fact"
    assert [e["source_id"] for e in rows[0]["evidence"]] == [src_a, src_b]
    assert rows[0]["evidence"][0] == {"source_id": src_a, "quote": "q1", "stance": "supports"}
    assert validate_topic(topic_dir) == []


def test_merge_is_idempotent_exact_duplicate(topic_dir, staged):
    cid, src_a = staged
    same = {"source_id": src_a, "quote": "q1", "stance": "supports"}
    assert merge_claim_evidence(topic_dir, "staging", cid, [same]) == [
        {"source_id": src_a, "result": "duplicate"}]
    # The same source with a different quote is a new record, not a duplicate.
    other = {"source_id": src_a, "quote": "q1 another paragraph", "stance": "supports"}
    assert merge_claim_evidence(topic_dir, "staging", cid, [same, other]) == [
        {"source_id": src_a, "result": "duplicate"}, {"source_id": src_a, "result": "added"}]
    assert len(_rows(topic_dir, "staging")[0]["evidence"]) == 2


def test_merge_in_final_grows_only(topic_dir, staged):
    cid, src_a = staged
    promote_claim(topic_dir, cid)
    src_b = _src(topic_dir, "https://example.com/b")
    merge_claim_evidence(topic_dir, "final", cid,
                         [{"source_id": src_b, "quote": "q2", "stance": "supports"}])
    final = _rows(topic_dir, "final")
    assert len(final) == 1 and len(final[0]["evidence"]) == 2
    assert final[0]["status"] == "verified" and _rows(topic_dir, "staging") == []
    assert validate_topic(topic_dir) == []


def test_merge_rejects_unknown_source_fk_and_bad_schema_before_write(topic_dir, staged):
    cid, src_a = staged
    before = _rows(topic_dir, "staging")
    with pytest.raises(ValidationError, match="FK"):
        merge_claim_evidence(topic_dir, "staging", cid,
                             [{"source_id": "src_000000000000", "quote": "q", "stance": "supports"}])
    with pytest.raises(ValidationError):
        merge_claim_evidence(topic_dir, "staging", cid,
                             [{"source_id": src_a, "quote": "q", "stance": "unknown"}])
    assert _rows(topic_dir, "staging") == before


def test_merge_refuses_retracted_missing_bad_zone_and_empty(topic_dir, staged):
    cid, src_a = staged
    entry = {"source_id": src_a, "quote": "q9", "stance": "supports"}
    with pytest.raises(KeyError):
        merge_claim_evidence(topic_dir, "staging", "clm_000000000000", [entry])
    with pytest.raises(KeyError):
        merge_claim_evidence(topic_dir, "final", cid, [entry])
    with pytest.raises(ValueError):
        merge_claim_evidence(topic_dir, "wiki", cid, [entry])
    with pytest.raises(ValueError):
        merge_claim_evidence(topic_dir, "staging", cid, [])
    retract_claim(topic_dir, "staging", cid)
    with pytest.raises(ValidationError, match="retracted"):
        merge_claim_evidence(topic_dir, "staging", cid, [entry])
    assert len(_rows(topic_dir, "staging")[0]["evidence"]) == 1


def test_cli_merge_evidence(topic_dir, staged):
    cid, src_a = staged
    src_b = _src(topic_dir, "https://example.com/b")
    payload = json.dumps([{"source_id": src_b, "quote": "q2", "stance": "mentions"}])
    res = subprocess.run([sys.executable, "-m", "llmwiki", "merge-evidence", str(topic_dir),
                          "staging", cid], input=payload, capture_output=True, text=True,
                         cwd=str(Path(__file__).parent.parent))
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [{"source_id": src_b, "result": "added"}]
    bad = subprocess.run([sys.executable, "-m", "llmwiki", "merge-evidence", str(topic_dir),
                          "staging", cid], input="{}", capture_output=True, text=True,
                         cwd=str(Path(__file__).parent.parent))
    assert bad.returncode != 0 and "array" in bad.stderr
