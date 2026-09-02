"""Filesystem primitives for atomic writes and a self-ignoring state directory.

Vendored from tool-template and derived from an orchestration database convention.
"""
from __future__ import annotations

import os
from pathlib import Path

# Self-ignore makes the directory invisible to Git in any repository, independent of the root
# .gitignore, so local state cannot enter `git add .` even in another project.
GITIGNORE_BODY = "# Tool state is not part of the repository.\n*\n"


def atomic_write(path: str | Path, data: str | bytes, encoding: str = "utf-8") -> None:
    """Write without partial states using an adjacent temporary file and atomic os.replace.

    A reader never sees a partially written file. A crashed process can leave only a temporary
    fragment, never a corrupted original.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding=encoding)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_state_dir(path: str | Path) -> Path:
    """Create a state directory with a self-ignoring .gitignore and return its Path.

    The operation is idempotent and never overwrites an existing .gitignore that its owner may have
    customized. Exclusive creation mode avoids clobbering during a race between parallel processes.
    """
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            with open(gi, "x", encoding="utf-8") as f:
                f.write(GITIGNORE_BODY)
        except FileExistsError:
            pass  # Another process created it between exists() and open(), which is the desired state.
    return d
