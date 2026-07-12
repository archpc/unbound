"""Strict request, response, and lightweight JSON-schema validation."""

from __future__ import annotations

import json
import re


CONFIDENCE_VALUES = ("low", "medium", "high")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SchemaError(ValueError):
    """Raised when external data does not match an expected schema."""


def validate_json_schema(value, schema, path="$"):
    """Validate the deliberately small JSON Schema subset used by tools."""
    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
    }
    if expected in type_map:
        py_type = type_map[expected]
        if not isinstance(value, py_type) or (
            expected in ("integer", "number") and isinstance(value, bool)
        ):
            raise SchemaError("{} must be {}".format(path, expected))
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError("{} is not an allowed value".format(path))
    if expected == "string":
        if len(value) < schema.get("minLength", 0):
            raise SchemaError("{} is too short".format(path))
        if len(value) > schema.get("maxLength", len(value)):
            raise SchemaError("{} is too long".format(path))
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaError("{} has an invalid format".format(path))
    elif expected == "integer":
        if value < schema.get("minimum", value):
            raise SchemaError("{} is below the minimum".format(path))
        if value > schema.get("maximum", value):
            raise SchemaError("{} exceeds the maximum".format(path))
    elif expected == "array":
        if len(value) > schema.get("maxItems", len(value)):
            raise SchemaError("{} has too many items".format(path))
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, "{}[{}]".format(path, index))
    elif expected == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError("{}.{} is required".format(path, key))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise SchemaError("{} has unsupported fields: {}".format(path, ", ".join(sorted(extra))))
        for key, child in value.items():
            if key in properties:
                validate_json_schema(child, properties[key], "{}.{}".format(path, key))
    return value


ASK_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["question"],
    "additionalProperties": False,
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}

ASK_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["answer", "confidence", "evidence", "next_checks", "limitations"],
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 12000},
        "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
        "evidence": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "required": ["tool", "summary"],
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string", "pattern": TOOL_NAME_RE.pattern},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
            },
        },
        "next_checks": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "limitations": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
    },
}


def parse_ask_request(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaError("request body must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError("request body must be valid UTF-8 JSON") from exc
    return validate_json_schema(value, ASK_REQUEST_SCHEMA)


def validate_ask_response(value, used_tools=None):
    validate_json_schema(value, ASK_RESPONSE_SCHEMA)
    if used_tools is not None:
        unknown = {item["tool"] for item in value["evidence"]} - set(used_tools)
        if unknown:
            raise SchemaError("evidence cites tools that were not run: {}".format(", ".join(sorted(unknown))))
    return value


def degraded_response(answer, limitation):
    return {
        "answer": answer,
        "confidence": "low",
        "evidence": [],
        "next_checks": ["Check the local gateway and model service logs."],
        "limitations": [limitation],
    }
