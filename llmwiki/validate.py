"""Validate against llmwiki/spec/*.schema.json as the single source of rules.

A small engine interprets a JSON Schema subset and fails closed. An unknown validation keyword is
an error, never a silent skip, because otherwise the specification and code could diverge unnoticed.
"""
import json
import re
from pathlib import Path

SPEC_DIR = Path(__file__).parent / "spec"

# Keywords understood by the engine. Any other schema keyword raises SpecError fail-closed.
_KNOWN = {"$schema", "$id", "title", "type", "required", "additionalProperties",
          "properties", "items", "minItems", "minLength", "pattern", "enum"}


class SpecError(Exception):
    """The schema uses an unsupported rule; extend the engine instead of ignoring the schema."""


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def load_schema(name: str) -> dict:
    schema = json.loads((SPEC_DIR / f"{name}.schema.json").read_text("utf-8"))
    _check_known(schema, name)
    return schema


def _check_known(node: dict, where: str) -> None:
    unknown = set(node) - _KNOWN
    if unknown:
        raise SpecError(f"{where}: unknown schema keywords {sorted(unknown)}")
    for key, sub in node.get("properties", {}).items():
        _check_known(sub, f"{where}.{key}")
    if "items" in node:
        _check_known(node["items"], f"{where}[]")


def _type_ok(value, expected: str) -> bool:
    m = {"object": dict, "array": list, "string": str}
    if expected not in m:
        raise SpecError(f"the engine does not support type={expected!r}")
    # bool is an int subclass in Python, but this engine supports only object, array, and string.
    return isinstance(value, m[expected])


def _validate(value, schema: dict, path: str, errors: list[str]) -> None:
    if "enum" in schema:
        if value not in schema["enum"]:
            errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
        return
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return
    if expected == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength={schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match {schema['pattern']}")
    elif expected == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems={schema['minItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{i}]", errors)
    elif expected == "object":
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                errors.append(f"{path}: unexpected fields {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", errors)


def validate_object(obj: dict, schema_name: str) -> None:
    """Raise ValidationError with every error, not only the first one."""
    errors: list[str] = []
    _validate(obj, load_schema(schema_name), schema_name, errors)
    if errors:
        raise ValidationError(errors)
