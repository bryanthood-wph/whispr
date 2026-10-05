"""A small JSON-Schema validator for the subset our schemas use.

Chosen over adding the `jsonschema` package so the distributed pipeline carries no
extra wheels. Any keyword outside SUPPORTED raises SchemaError instead of being
ignored, so a schema can never silently rely on a check that isn't enforced.
"""

from __future__ import annotations

from typing import Any

ANNOTATIONS = {"$schema", "$id", "title", "description", "default", "examples", "$comment", "$defs"}
SUPPORTED = ANNOTATIONS | {
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minimum", "maximum", "minLength", "minItems", "maxItems", "$ref",
}

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """The schema itself uses something this validator does not enforce."""


def _types(schema: dict) -> list[str]:
    t = schema.get("type", [])
    return t if isinstance(t, list) else [t]


def check_schema(schema: dict, path: str = "#") -> None:
    """Walk every node of the schema, not just the ones an instance reaches, so an
    unsupported keyword under an empty array or an absent property still raises."""
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"unsupported schema keyword(s) at {path}: {sorted(unknown)}")
    for t in _types(schema):
        if t not in _TYPES:
            raise SchemaError(f"unknown type {t!r} at {path}")
    for group in ("properties", "$defs"):
        for name, sub in schema.get(group, {}).items():
            check_schema(sub, f"{path}/{group}/{name}")
    if "items" in schema:
        if not isinstance(schema["items"], dict):
            raise SchemaError(f"only a single-schema 'items' is supported at {path}")
        check_schema(schema["items"], f"{path}/items")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema(schema["additionalProperties"], f"{path}/additionalProperties")


def validate(instance: Any, schema: dict) -> list[str]:
    """Return a list of error strings ("<path>: <problem>"); empty means valid."""
    check_schema(schema)
    errors: list[str] = []
    _check(instance, schema, schema, "$", errors)
    return errors


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise SchemaError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check(value: Any, schema: dict, root: dict, path: str, errors: list[str]) -> None:
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, path, errors)
        return
    if "type" in schema:
        types = _types(schema)
        if not any(_TYPES[t](value) for t in types):
            errors.append(f"{path}: expected {'/'.join(types)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(item, schema["items"], root, f"{path}[{i}]", errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _check(item, props[key], root, f"{path}.{key}", errors)
            elif extra is False:
                errors.append(f"{path}: unknown key {key!r}")
            elif isinstance(extra, dict):
                _check(item, extra, root, f"{path}.{key}", errors)
