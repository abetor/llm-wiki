"""Isolate tests from user files and credentials by replacing HOME/cwd and cleaning the environment.

A test that accidentally reads the real $HOME for configuration or state, or writes into cwd, can
silently damage its owner's machine. Prevent that structurally instead of relying on discipline.
Autouse fixtures make every test hermetic by construction.
"""
import re
import sys
from pathlib import Path

import pytest

# Import the package without installation so tests can run from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Hide environment variables declared in .env.sample, including commented examples, from tests.
# This prevents a real token inherited from the process from leaking into a supposedly hermetic
# test that calls a live API locally and fails on a clean machine. Conservatively remove only names
# declared in .env.sample rather than guessing across os.environ, because incorrectly removing an
# unrelated PATH-like variable is more dangerous than missing an undeclared name.
_ENV_VAR_RE = re.compile(r"([A-Z][A-Z0-9_]*)=")


def _env_sample_vars(sample: Path | None = None) -> list[str]:
    """Return variable names from .env.sample lines formatted as optional-comment NAME=value."""
    if sample is None:
        sample = Path(__file__).resolve().parents[1] / ".env.sample"
    if not sample.is_file():
        return []
    names = []
    for line in sample.read_text("utf-8").splitlines():
        m = _ENV_VAR_RE.match(line.lstrip("# ").strip())
        if m:
            names.append(m.group(1))
    return names


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # The underscore is deliberate. tmp_path/"home" conflicted with tests that created their own
    # directory named "home" and produced FileExistsError in an early run.
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for name in _env_sample_vars():
        monkeypatch.delenv(name, raising=False)
