"""On-disk topic structure. A topic is a complete, portable research state.

There is no global topics database. base_dir may be any directory, with a topic beneath it:

    <base>/<slug>/
      topic.json          manifest (schema: topic)
      sources/
        sources.jsonl     topic-level source registry (schema: source)
        content/          content referenced by work, staging, and final
      work/               engine workspace for plans, steps, and checkpoints; not validated
      staging/claims.jsonl  candidate claims after verification (schema: claim)
      final/claims.jsonl    clean final claims (schema: claim plus zone status rule)
      final/wiki/           synthesis by concept as <concept>.md with frontmatter (schema: wiki)
"""
import datetime
import json
import re
from pathlib import Path

from .validate import validate_object

ZONES = ("work", "staging", "final")


def slugify(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not slug:
        raise ValueError(f"topic {topic!r} does not produce a slug")
    return slug


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def init_topic(base_dir: str | Path, topic: str, slug: str | None = None,
               description: str = "") -> Path:
    """Create a topic structure; a repeated call for an existing topic fails instead of overwriting."""
    slug = slug or slugify(topic)
    root = Path(base_dir) / slug
    manifest_path = root / "topic.json"
    if manifest_path.exists():
        raise FileExistsError(f"topic already exists: {manifest_path}")
    manifest = {"spec_version": "0.1.0", "topic": topic, "slug": slug, "created_at": _now()}
    if description:
        manifest["description"] = description
    validate_object(manifest, "topic")
    for zone in ZONES:
        (root / zone).mkdir(parents=True, exist_ok=True)
    (root / "sources" / "content").mkdir(parents=True, exist_ok=True)
    (root / "final" / "wiki").mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return root


def find_topic(topic_dir: str | Path) -> dict:
    """Read and validate the manifest, also confirming that the directory is a topic."""
    manifest = json.loads((Path(topic_dir) / "topic.json").read_text("utf-8"))
    validate_object(manifest, "topic")
    return manifest
