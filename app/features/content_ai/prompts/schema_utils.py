"""Anthropic-compatible JSON Schema validation for contract tests."""

from __future__ import annotations

from typing import Any

_FORBIDDEN_CONSTRAINTS = frozenset(
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minProperties",
        "maxProperties",
    }
)

_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})


def assert_anthropic_compatible(schema: dict[str, Any], *, path: str = "$") -> None:
    """Recursively validate *schema* against Anthropic structured-output constraints."""
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema node must be an object")

    if "$ref" in schema:
        raise ValueError(f"{path}.$ref: external or internal $ref is not allowed")

    for key in ("$defs", "definitions"):
        if key in schema:
            raise ValueError(f"{path}.{key}: recursive schema definitions are not allowed")

    for key in _FORBIDDEN_CONSTRAINTS:
        if key in schema:
            raise ValueError(f"{path}.{key}: numeric or length constraints are not allowed")

    min_items = schema.get("minItems")
    if min_items is not None and min_items not in (0, 1):
        raise ValueError(f"{path}.minItems: only 0 or 1 is allowed, got {min_items!r}")

    if "enum" in schema:
        for index, value in enumerate(schema["enum"]):
            if isinstance(value, (dict, list)):
                raise ValueError(
                    f"{path}.enum[{index}]: enum values must be scalar, got {type(value).__name__}"
                )

    node_type = schema.get("type")
    if node_type is not None:
        types = [node_type] if isinstance(node_type, str) else list(node_type)
        for type_name in types:
            if type_name not in _SCALAR_TYPES and type_name != "object" and type_name != "array":
                raise ValueError(f"{path}.type: unsupported type {type_name!r}")

        if "object" in types:
            if schema.get("additionalProperties") is not False:
                raise ValueError(f"{path}: object schemas must set additionalProperties: false")
            properties = schema.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError(f"{path}.properties: must be an object")
            # OpenAI strict structured outputs require `required` to list every property key.
            required = schema.get("required")
            if not isinstance(required, list):
                raise ValueError(f"{path}.required: must be an array of every property key")
            missing = sorted(set(properties) - set(required))
            if missing:
                raise ValueError(
                    f"{path}.required: must include every property key; missing {missing}"
                )
            for prop_name, prop_schema in properties.items():
                assert_anthropic_compatible(prop_schema, path=f"{path}.properties.{prop_name}")

        if "array" in types:
            items = schema.get("items")
            if not isinstance(items, dict):
                raise ValueError(f"{path}.items: array schemas must declare an items object")
            assert_anthropic_compatible(items, path=f"{path}.items")

    for keyword in ("properties", "patternProperties", "allOf", "anyOf", "oneOf"):
        nested = schema.get(keyword)
        if nested is None:
            continue
        if keyword in ("allOf", "anyOf", "oneOf"):
            if not isinstance(nested, list):
                raise ValueError(f"{path}.{keyword}: must be an array")
            for index, sub_schema in enumerate(nested):
                assert_anthropic_compatible(sub_schema, path=f"{path}.{keyword}[{index}]")
        elif isinstance(nested, dict):
            for name, sub_schema in nested.items():
                assert_anthropic_compatible(sub_schema, path=f"{path}.{keyword}.{name}")
