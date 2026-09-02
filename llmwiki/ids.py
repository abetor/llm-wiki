"""Deterministic ids make identical content produce the same id for exact deduplication.

Paths are never ids because renames would break them. Identifiers use only content hashes.
"""
import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _h12(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def make_source_id(kind: str, url: str | None, title: str) -> str:
    # URL is the primary source identity; without one, such as for a book or local file, use kind+title.
    key = url.strip() if url else f"{kind}\n{normalize_text(title)}"
    return "src_" + _h12(key)


def make_claim_id(slug: str, text: str) -> str:
    return "clm_" + _h12(f"{slug}\n{normalize_text(text)}")


def make_wiki_id(slug: str, title: str) -> str:
    # Topic plus concept title makes the same concept in a topic resolve to the same id and page.
    return "wik_" + _h12(f"{slug}\n{normalize_text(title)}")
