"""llmwiki is a knowledge engine combining a format specification with a write socket.

The socket is the executable form of the specification. It validates against schemas in
llmwiki/spec as the single source of truth, avoids duplicating rules in code, deduplicates records,
and never admits invalid input. Direct file reads remain supported, while ext.query provides a
read layer because production use showed that ad hoc consumer readers cause the same class of
silent errors as ad hoc writes.
"""
from .topic import init_topic, find_topic
from .store import (add_source, update_source, add_claim, add_claims, promote_claim,
                    promote_claims, retract_claim, set_claim_status, merge_claim_evidence,
                    add_wiki_page, update_wiki_page, put_wiki_page, delete_wiki_page,
                    wiki_page_path, validate_topic, DuplicateClaim, LockTimeout)
from .frontmatter import parse_page, render_page, FrontmatterError
from .ids import make_source_id, make_claim_id, make_wiki_id
from .ext.query import (read_claims, read_sources, read_pages, list_claims,
                        stats, audit)

SPEC_VERSION = "0.1.0"

__all__ = [
    "SPEC_VERSION", "init_topic", "find_topic",
    "add_source", "update_source", "add_claim", "add_claims", "promote_claim",
    "promote_claims", "retract_claim", "set_claim_status", "merge_claim_evidence",
    "validate_topic", "DuplicateClaim", "LockTimeout",
    "add_wiki_page", "update_wiki_page", "put_wiki_page", "delete_wiki_page",
    "wiki_page_path",
    "parse_page", "render_page", "FrontmatterError",
    "make_source_id", "make_claim_id", "make_wiki_id",
    "read_claims", "read_sources", "read_pages", "list_claims", "stats", "audit",
]
