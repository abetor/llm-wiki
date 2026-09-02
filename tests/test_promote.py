import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import (add_claim, add_source, init_topic, promote_claim,  # noqa: E402
                     retract_claim, validate_topic)
from llmwiki.validate import ValidationError  # noqa: E402


@pytest.fixture
def topic_dir(tmp_path):
    return init_topic(tmp_path, "W2 promotion topic")


def _src(topic_dir):
    return add_source(topic_dir, kind="web", title="Article", url="https://example.com/a")


def _staged(topic_dir, text="Fact from staging", status="verified"):
    src = _src(topic_dir)
    return add_claim(topic_dir, "staging", text=text, confidence="high", status=status,
                     evidence=[{"source_id": src, "quote": "q", "stance": "supports"}])


def _rows(topic_dir, zone):
    p = topic_dir / zone / "claims.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


# --- promote: move a complete row ---

def test_promote_moves_row_out_of_staging(topic_dir):
    cid = _staged(topic_dir)
    assert promote_claim(topic_dir, cid) == cid
    # The row disappears from staging and appears in final.
    assert _rows(topic_dir, "staging") == []
    final = _rows(topic_dir, "final")
    assert len(final) == 1 and final[0]["id"] == cid
    # Promotion does not change status because verified is legal in final.
    assert final[0]["status"] == "verified"
    assert validate_topic(topic_dir) == []


def test_promote_keeps_other_staging_rows(topic_dir):
    src = _src(topic_dir)
    a = add_claim(topic_dir, "staging", text="First", confidence="high", status="verified",
                  evidence=[{"source_id": src, "quote": "q", "stance": "supports"}])
    b = add_claim(topic_dir, "staging", text="Second", confidence="low", status="candidate",
                  evidence=[{"source_id": src, "quote": "q", "stance": "mentions"}])
    promote_claim(topic_dir, a)
    staging_ids = {r["id"] for r in _rows(topic_dir, "staging")}
    assert staging_ids == {b}  # The candidate that was not promoted remains in place.
    assert {r["id"] for r in _rows(topic_dir, "final")} == {a}


def test_promote_rejects_candidate(topic_dir):
    cid = _staged(topic_dir, status="candidate")
    with pytest.raises(ValidationError, match="only verified"):
        promote_claim(topic_dir, cid)
    # Nothing was moved.
    assert len(_rows(topic_dir, "staging")) == 1
    assert _rows(topic_dir, "final") == []


def test_promote_rejects_retracted(topic_dir):
    cid = _staged(topic_dir)
    retract_claim(topic_dir, "staging", cid)
    with pytest.raises(ValidationError, match="only verified"):
        promote_claim(topic_dir, cid)


def test_promote_missing_raises(topic_dir):
    with pytest.raises(KeyError):
        promote_claim(topic_dir, "clm_000000000000")


def test_promote_twice_second_has_nothing(topic_dir):
    cid = _staged(topic_dir)
    promote_claim(topic_dir, cid)
    with pytest.raises(KeyError):  # Staging is already empty.
        promote_claim(topic_dir, cid)
    assert len(_rows(topic_dir, "final")) == 1  # No duplicate appeared in final.


def test_promote_reconciles_claim_already_in_final(topic_dir):
    # Manually add the same text to staging as verified and final as final, yielding one id in both zones.
    src = _src(topic_dir)
    ev = [{"source_id": src, "quote": "q", "stance": "supports"}]
    a = add_claim(topic_dir, "staging", text="Duplicate", confidence="high", status="verified",
                  evidence=ev)
    b = add_claim(topic_dir, "final", text="Duplicate", confidence="high", status="final",
                  evidence=ev)
    assert a == b
    promote_claim(topic_dir, a)
    assert _rows(topic_dir, "staging") == []      # The staging copy was removed.
    final = _rows(topic_dir, "final")
    assert len(final) == 1 and final[0]["status"] == "final"  # Final is authoritative and unchanged.
    assert validate_topic(topic_dir) == []


def test_promote_refuses_corrupt_staging_row(topic_dir):
    # Manual staging corruption must not pass through promotion into final.
    cid = _staged(topic_dir)
    path = topic_dir / "staging" / "claims.jsonl"
    row = json.loads(path.read_text("utf-8"))
    row["confidence"] = "absolute"  # Not in the enum.
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", "utf-8")
    with pytest.raises(ValidationError):
        promote_claim(topic_dir, cid)
    assert _rows(topic_dir, "final") == []  # Nothing leaked into final.


# --- retract: retain the row and change status to retracted ---

def test_retract_in_staging_keeps_row(topic_dir):
    cid = _staged(topic_dir)
    assert retract_claim(topic_dir, "staging", cid) == cid
    rows = _rows(topic_dir, "staging")
    assert len(rows) == 1  # The row remains as history.
    assert rows[0]["status"] == "retracted"
    assert validate_topic(topic_dir) == []


def test_retract_in_final_is_legal_mutation(topic_dir):
    # Retraction is the only legal mutation of an existing final row.
    src = _src(topic_dir)
    cid = add_claim(topic_dir, "final", text="Final fact", confidence="high", status="final",
                    evidence=[{"source_id": src, "quote": "q", "stance": "supports"}])
    retract_claim(topic_dir, "final", cid)
    rows = _rows(topic_dir, "final")
    assert len(rows) == 1 and rows[0]["status"] == "retracted"
    # Retracted is valid in final for the topic validation engine.
    assert validate_topic(topic_dir) == []


def test_retract_idempotent(topic_dir):
    cid = _staged(topic_dir)
    retract_claim(topic_dir, "staging", cid)
    retract_claim(topic_dir, "staging", cid)  # A repeated call is a no-op.
    rows = _rows(topic_dir, "staging")
    assert len(rows) == 1 and rows[0]["status"] == "retracted"


def test_retract_missing_raises(topic_dir):
    with pytest.raises(KeyError):
        retract_claim(topic_dir, "final", "clm_000000000000")


def test_retract_bad_zone_raises(topic_dir):
    cid = _staged(topic_dir)
    with pytest.raises(ValueError):
        retract_claim(topic_dir, "work", cid)


def test_promote_then_retract_in_final(topic_dir):
    # End-to-end path: verified in staging, promoted, then retracted in final.
    cid = _staged(topic_dir)
    promote_claim(topic_dir, cid)
    retract_claim(topic_dir, "final", cid)
    final = _rows(topic_dir, "final")
    assert final[0]["status"] == "retracted"
    assert _rows(topic_dir, "staging") == []
    assert validate_topic(topic_dir) == []


# --- CLI ---

def test_cli_promote_retract_smoke(tmp_path):
    env_dir = Path(__file__).parent.parent
    root = init_topic(tmp_path, "CLI W2 promote")
    src = add_source(root, kind="web", title="t", url="https://e.com")
    cid = add_claim(root, "staging", text="CLI fact", confidence="high", status="verified",
                    evidence=[{"source_id": src, "quote": "q", "stance": "supports"}])

    r = subprocess.run([sys.executable, "-m", "llmwiki", "promote", str(root), cid],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == cid
    assert _rows(root, "staging") == [] and len(_rows(root, "final")) == 1

    r = subprocess.run([sys.executable, "-m", "llmwiki", "retract", str(root), "final", cid],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0, r.stderr
    assert _rows(root, "final")[0]["status"] == "retracted"

    r = subprocess.run([sys.executable, "-m", "llmwiki", "validate", str(root)],
                       capture_output=True, text=True, cwd=env_dir)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr
