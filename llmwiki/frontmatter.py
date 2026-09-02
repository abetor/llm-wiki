"""Minimal YAML frontmatter supporting exactly the required subset with no dependencies.

Supported forms are scalar strings (`key: value`) and block-style string lists (`  - item`). Every
value is a string: the engine never guesses types, so `title: 123` yields string "123", not a number.
Unparseable input raises FrontmatterError rather than guessing, which prevents silent divergence
between data and schema.

The codec is paired: parse_frontmatter(dump_frontmatter(x)) == x for valid values. Ambiguous values
with quotes, newlines, or edge whitespace serialize as double-quoted JSON strings. This remains a
valid YAML subset that the parser recognizes by its leading quote.
"""
import json
import re

FENCE = "---"

_KEY_SCALAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]+(.+?)[ \t]*$")
_KEY_LIST = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*$")
_ITEM = re.compile(r"^[ \t]+-[ \t]+(.+?)[ \t]*$")


class FrontmatterError(Exception):
    """Frontmatter does not fit the supported subset; repair input instead of guessing intent."""


def _parse_scalar(raw: str) -> str:
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FrontmatterError(f"quoted value is not valid JSON: {raw!r} ({e})")
        if not isinstance(value, str):
            raise FrontmatterError(f"expected a string, got {type(value).__name__}: {raw!r}")
        return value
    return raw


def _is_plain_safe(s: str) -> bool:
    # Plain representation is round-trip safe only if the parser returns exactly this string: no
    # edge whitespace stripped by the regex, no control characters including newline or tab, and
    # no leading quote that would make the parser treat it as a JSON string.
    return bool(s) and s == s.strip() and not s.startswith('"') \
        and all(ord(c) >= 0x20 for c in s)


def _dump_scalar(s: str) -> str:
    return s if _is_plain_safe(s) else json.dumps(s, ensure_ascii=False)


def parse_frontmatter(fm: str) -> dict:
    data: dict = {}
    current_list: str | None = None  # Key receiving subsequent '- item' entries.
    for n, line in enumerate(fm.splitlines(), 1):
        if not line.strip():
            continue
        m = _ITEM.match(line)
        if m:
            if current_list is None:
                raise FrontmatterError(f"line {n}: list item without a list key: {line!r}")
            data[current_list].append(_parse_scalar(m.group(1)))
            continue
        m = _KEY_SCALAR.match(line)
        if m:
            key = m.group(1)
            if key in data:
                raise FrontmatterError(f"line {n}: duplicate key {key!r}")
            data[key] = _parse_scalar(m.group(2))
            current_list = None
            continue
        m = _KEY_LIST.match(line)
        if m:
            key = m.group(1)
            if key in data:
                raise FrontmatterError(f"line {n}: duplicate key {key!r}")
            data[key] = []
            current_list = key
            continue
        raise FrontmatterError(f"line {n}: not parseable as the supported YAML subset: {line!r}")
    return data


def dump_frontmatter(data: dict) -> str:
    out: list[str] = []
    for key, val in data.items():
        if isinstance(val, list):
            out.append(f"{key}:")
            for item in val:
                if not isinstance(item, str):
                    raise FrontmatterError(f"{key}: list item is not a string: {item!r}")
                out.append(f"  - {_dump_scalar(item)}")
        elif isinstance(val, str):
            out.append(f"{key}: {_dump_scalar(val)}")
        else:
            raise FrontmatterError(
                f"{key}: only strings and lists of strings are supported, not {type(val).__name__}")
    return "".join(line + "\n" for line in out)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Separate frontmatter from the Markdown body; missing paired `---` raises FrontmatterError."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        raise FrontmatterError("missing opening '---': this is not a wiki page")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == FENCE:
            fm = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            return fm, body.strip("\n")
    raise FrontmatterError("missing closing '---' for frontmatter")


def parse_page(text: str) -> tuple[dict, str]:
    fm, body = split_frontmatter(text)
    return parse_frontmatter(fm), body


def render_page(data: dict, body: str = "") -> str:
    fm = dump_frontmatter(data)
    body = body.strip("\n")
    doc = f"{FENCE}\n{fm}{FENCE}\n"
    if body:
        doc += "\n" + body + "\n"
    return doc
