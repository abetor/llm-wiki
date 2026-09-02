"""CLI smoke tests prove that doctor runs, never mutates, and reports failures honestly.

llm-wiki has no --home because it owns no data and receives every external topic directory as an
argument. doctor therefore checks the socket's only environment requirement: the fail-closed
engine understands every specification schema. Installed-package checks live in smoke/, where
exit 77 means skip.
"""
import json

import pytest

import llmwiki.validate
from llmwiki.cli import EXIT_FAIL, EXIT_OK, main


def _spec_copy(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    for name in ("topic", "source", "claim", "wiki"):
        src = llmwiki.validate.SPEC_DIR / f"{name}.schema.json"
        (spec / src.name).write_text(src.read_text("utf-8"), "utf-8")
    return spec


def test_doctor_ok(capsys):
    assert main(["doctor"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "doctor" in out and "READY" in out


def test_doctor_mutates_nothing(tmp_path, monkeypatch):
    cwd = tmp_path / "empty-cwd"  # Separate because the fixture places _isolated_home in tmp_path.
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert main(["doctor"]) == EXIT_OK
    assert list(cwd.iterdir()) == []  # doctor diagnoses without mutation.


def test_doctor_fail_closed_on_unknown_schema_keyword(tmp_path, monkeypatch, capsys):
    # Anti-vacuity check: a schema keyword unknown to the engine must fail doctor. Otherwise the
    # command could report success while checking nothing meaningful.
    spec = _spec_copy(tmp_path)
    bad = spec / "claim.schema.json"
    bad.write_text(bad.read_text("utf-8").replace('"type": "object"',
                                                  '"type": "object", "if": {}', 1), "utf-8")
    monkeypatch.setattr(llmwiki.validate, "SPEC_DIR", spec)
    assert main(["doctor"]) == EXIT_FAIL
    assert "NOT READY" in capsys.readouterr().out


def test_doctor_covers_schema_not_listed_in_code(tmp_path, monkeypatch, capsys):
    # A new file in spec/ must enter the gate automatically. A hard-coded name list can be left
    # unchanged, leaving a specification rule unenforced.
    spec = _spec_copy(tmp_path)
    (spec / "run.schema.json").write_text(
        json.dumps({"type": "object", "if": {}}, ensure_ascii=False), "utf-8")
    monkeypatch.setattr(llmwiki.validate, "SPEC_DIR", spec)
    assert main(["doctor"]) == EXIT_FAIL
    out = capsys.readouterr().out
    assert "run.schema.json" in out and "NOT READY" in out


def test_doctor_red_on_missing_core_schema(tmp_path, monkeypatch, capsys):
    # An empty or truncated specification is a failure, not an excuse to report nothing to check.
    spec = _spec_copy(tmp_path)
    (spec / "claim.schema.json").unlink()
    monkeypatch.setattr(llmwiki.validate, "SPEC_DIR", spec)
    assert main(["doctor"]) == EXIT_FAIL
    assert "claim" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    pytest.param(["no-such-cmd"], id="unknown-subcommand"),
    pytest.param([], id="missing-subcommand"),
    pytest.param(["init", "--no-such-flag"], id="invalid-flag"),
    pytest.param(["init", "/tmp/base"], id="missing-arguments"),
    pytest.param(["add-claim", "/tmp/topic", "middle"], id="invalid-zone"),
])
def test_usage_errors_exit_1_not_2(argv):
    # The contract defines only 0 and 1. Stock argparse leaked its own 2 until shared.cliargs
    # normalized usage failures.
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == EXIT_FAIL
