"""verify-quotes covers three observed quote/locator discrepancies and a fabricated quote.

Cases come from course-run failure logs: an ASR paragraph break, a PDF marker line instead of
content, `<br>` from an HTML converter, and a genuinely nonexistent claim that alone must fail the
batch.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from llmwiki import add_claim, add_source, init_topic, retract_claim  # noqa: E402
from llmwiki.cli import EXIT_FAIL, EXIT_OK  # noqa: E402
from llmwiki.ext.evidence import verify_quotes  # noqa: E402

TRANSCRIPT = "\n".join([
    "Today we examine agent memory.",                         # 1
    "",                                                        # 2
    "The metric is simple: how many steps the system takes",   # 3
    "before human intervention.",                              # 4
    "",                                                        # 5
    "<!-- Start of picture text -->",                          # 6
    "Observability measures sessions rather than runs.",       # 7
    "Separate read<br>from retrieve.",                         # 8
])


@pytest.fixture
def topic(tmp_path):
    root = init_topic(tmp_path, "Topic", slug="t")
    (root / "sources" / "content").mkdir(parents=True, exist_ok=True)
    (root / "sources" / "content" / "001.md").write_text(TRANSCRIPT, "utf-8")
    src = add_source(root, kind="local", title="Lesson 001",
                     content_path="sources/content/001.md")
    return root, src


def _put(root, src, quote, locator, text="Claim"):
    ev = {"source_id": src, "quote": quote, "stance": "supports"}
    if locator:
        ev["locator"] = {"type": "line", "value": locator}
    return add_claim(root, "staging", text=text, confidence="high", status="verified",
                     evidence=[ev])


def test_citata_cherez_asr_razryv_schitaetsya_doslovnoj(topic):
    """A sentence crosses a physical line break that becomes a space in JSON."""
    root, src = topic
    _put(root, src, "The metric is simple: how many steps the system takes before human intervention.",
         "3-4")
    assert verify_quotes(root) == []


def test_locator_na_stroku_marker_eto_warn_a_ne_error(topic):
    """The PDF extractor points to a marker line while text begins on the next line."""
    root, src = topic
    _put(root, src, "Observability measures sessions rather than runs.", "6-7")
    assert verify_quotes(root) == []   # 6-7 intersects the actual 7-7 range.
    _put(root, src, "Observability measures sessions rather than runs.", "6-6", text="Second claim")
    found = [f for f in verify_quotes(root) if f["code"] == "locator-mismatch"]
    assert found and found[0]["suggested_locator"] == "7-7"
    assert found[0]["level"] == "warn"


def test_html_perenos_ne_lomaet_doslovnost(topic):
    root, src = topic
    _put(root, src, "Separate read from retrieve.", "8")
    assert verify_quotes(root) == []


def test_vydumannaya_citata_eto_error(topic):
    root, src = topic
    _put(root, src, "The course always recommends a vector database.", "3-4")
    found = verify_quotes(root)
    assert [f["code"] for f in found] == ["quote-not-found"]
    assert found[0]["level"] == "error"


def test_locator_otsutstvuet_podskazka_est(topic):
    root, src = topic
    _put(root, src, "Today we examine agent memory.", None)
    found = verify_quotes(root)
    assert found[0]["code"] == "locator-missing"
    assert found[0]["suggested_locator"] == "1-1"


def test_istochnik_bez_content_path_eto_error(tmp_path):
    root = init_topic(tmp_path, "Topic", slug="t2")
    src = add_source(root, kind="web", title="Article", url="https://example.com/a")
    add_claim(root, "staging", text="Claim", confidence="high", status="verified",
              evidence=[{"source_id": src, "quote": "something", "stance": "supports"}])
    assert [f["code"] for f in verify_quotes(root)] == ["source-unreadable"]


def test_retracted_vidno_kak_info_i_ne_ronyaet_gejt(topic):
    """A retraction is resolved history: its finding remains visible without failing the gate.

    In production, 65 sources were repointed to corrected transcripts and claims with displaced
    quotes were retracted. No broken live claim remained, yet verify-quotes returned exit 1 for 330
    tombstone findings. That permanent red background made new failures indistinguishable.
    """
    root, src = topic
    dead = _put(root, src, "This sentence does not occur in the source.", "1", text="Fabrication")
    assert [(f["code"], f["level"]) for f in verify_quotes(root)] == \
        [("quote-not-found", "error")]

    retract_claim(root, "staging", dead)
    found = verify_quotes(root)
    # History stays visible: the finding remains with a different level and reason.
    assert [(f["code"], f["level"], f["status"]) for f in found] == \
        [("quote-not-found", "info", "retracted")]

    def run():
        return subprocess.run([sys.executable, "-m", "llmwiki", "verify-quotes", str(root)],
                              capture_output=True, text=True,
                              cwd=str(Path(__file__).parent.parent))

    assert run().returncode == EXIT_OK
    # The gate still hears a broken live quote next to the tombstone and reports an error.
    _put(root, src, "This other sentence is also absent.", "1", text="Fresh fabrication")
    r = run()
    assert r.returncode == EXIT_FAIL
    levels = sorted(f["level"] for f in json.loads(r.stdout))
    assert levels == ["error", "info"]


def test_retracted_ponizhaet_i_warn(topic):
    """Every level is lowered, not only error, because a historical warning is not a live signal."""
    root, src = topic
    dead = _put(root, src, "Today we examine agent memory.", None)
    assert [f["level"] for f in verify_quotes(root)] == ["warn"]
    retract_claim(root, "staging", dead)
    found = verify_quotes(root)
    assert [(f["code"], f["level"]) for f in found] == [("locator-missing", "info")]
    assert found[0]["suggested_locator"] == "1-1"   # The suggestion remains present.


def test_cli_verify_quotes_exit_kody(topic):
    root, src = topic
    _put(root, src, "Today we examine agent memory.", "1")

    def run():
        return subprocess.run([sys.executable, "-m", "llmwiki", "verify-quotes", str(root)],
                              capture_output=True, text=True,
                              cwd=str(Path(__file__).parent.parent))

    r = run()
    assert r.returncode == EXIT_OK and json.loads(r.stdout) == []
    _put(root, src, "This sentence does not occur in the source.", "1", text="Fabrication")
    r = run()
    assert r.returncode == EXIT_FAIL
    assert any(f["code"] == "quote-not-found" for f in json.loads(r.stdout))
