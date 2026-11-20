"""Standalone validator for the AuditLab public JSON Schemas. Python standard library only.

It deliberately imports nothing from AuditLab, so external adapters can copy or load this file
by path and check the wire format independently of the implementation it describes.

Supported subset of JSON Schema Draft 2020-12 (anything else is refused, never ignored):
  $schema $id $comment title description $defs $ref(local "#/...") type enum const
  properties required additionalProperties propertyNames items(prefix rest) prefixItems
  minItems maxItems minLength maxLength pattern minimum maximum oneOf anyOf allOf not,
  and the boolean schemas true/false.
Semantics that differ from a generic validator, on purpose:
  * "integer" accepts only JSON numbers without fraction or exponent (2.0 is not an integer);
  * booleans are never numbers, so const 0 does not match false;
  * NaN/Infinity and duplicate object keys are rejected by load_json();
  * instances nested deeper than MAX_DEPTH are reported as invalid instead of crashing.

CLI:
  python schemas/validate.py SCHEMA_FILE DEFINITION INSTANCE_FILE|- [--jsonl]
  e.g. python schemas/validate.py schemas/public/1.0/auditlab-public.schema.json action action.json
DEFINITION is a name in $defs (action, stepTrain, ...) or a full pointer such as #/$defs/action.
Exit code 0 when every instance is valid, 1 when one is not, 2 on a bad schema, pointer or input file.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

# Validation safety cap, above the runner's 64-level action limit so that a trajectory line
# (line -> action -> ...) wrapping a maximal accepted action still validates.
MAX_DEPTH = 80
KEYWORDS = frozenset({
    "$schema", "$id", "$comment", "title", "description", "$defs", "$ref", "type", "enum", "const",
    "properties", "required", "additionalProperties", "propertyNames", "items", "prefixItems",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "minimum", "maximum",
    "oneOf", "anyOf", "allOf", "not",
})
TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})


class SchemaError(ValueError):
    """The schema document itself uses something this validator does not implement."""


def load_json(text: str):
    """Strict JSON: no NaN/Infinity, no duplicate keys."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise ValueError(f"non-finite number {name}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _is_type(value, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or \
            (isinstance(value, float) and math.isfinite(value))
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type {name!r}")


def _equal(left, right) -> bool:
    """JSON equality: booleans are not numbers, numbers compare by value."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_type(left, "number") and _is_type(right, "number"):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k]) for k in left)
    return type(left) is type(right) and left == right


def _depth(value, limit: int) -> bool:
    """True if value nests deeper than limit (iterative, so hostile input cannot overflow the stack)."""
    stack = [(value, 1)]
    while stack:
        item, level = stack.pop()
        if level > limit:
            return True
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return False


class Validator:
    def __init__(self, document: dict):
        if not isinstance(document, dict):
            raise SchemaError("schema document must be an object")
        self.document = document
        self._patterns: dict[str, re.Pattern] = {}
        self._check_schema(document, "#")

    @classmethod
    def from_path(cls, path) -> "Validator":
        return cls(load_json(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------ schema well-formedness
    def _check_schema(self, schema, where: str) -> None:
        if isinstance(schema, bool):
            return
        if not isinstance(schema, dict):
            raise SchemaError(f"{where}: a schema must be an object or a boolean")
        unknown = set(schema) - KEYWORDS
        if unknown:
            raise SchemaError(f"{where}: unsupported keywords {sorted(unknown)}")
        if "$ref" in schema:
            self._resolve(schema["$ref"])
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not names or any(name not in TYPES for name in names):
                raise SchemaError(f"{where}: bad type {schema['type']!r}")
        if "pattern" in schema:
            self._patterns[schema["pattern"]] = re.compile(schema["pattern"])
        if "required" in schema and not all(isinstance(k, str) for k in schema["required"]):
            raise SchemaError(f"{where}: required must list strings")
        if "enum" in schema and not isinstance(schema["enum"], list):
            raise SchemaError(f"{where}: enum must be an array")
        for key in ("$defs", "properties"):
            for name, child in schema.get(key, {}).items():
                self._check_schema(child, f"{where}/{key}/{name}")
        for key in ("additionalProperties", "propertyNames", "items", "not"):
            if key in schema:
                self._check_schema(schema[key], f"{where}/{key}")
        for key in ("prefixItems", "oneOf", "anyOf", "allOf"):
            if key in schema:
                if not isinstance(schema[key], list) or not schema[key]:
                    raise SchemaError(f"{where}/{key}: must be a non-empty array")
                for index, child in enumerate(schema[key]):
                    self._check_schema(child, f"{where}/{key}/{index}")

    def _resolve(self, pointer: str):
        if not isinstance(pointer, str) or not (pointer == "#" or pointer.startswith("#/")):
            raise SchemaError(f"only local references are supported: {pointer!r}")
        node = self.document
        for part in pointer[2:].split("/") if pointer != "#" else []:
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"unresolvable reference {pointer!r}")
            node = node[part]
        return node

    # ------------------------------------------------------------ instance validation
    def errors(self, instance, pointer: str = "#") -> list[str]:
        if _depth(instance, MAX_DEPTH):
            return [f"$: nesting deeper than {MAX_DEPTH} levels"]
        found: list[str] = []
        self._validate(instance, self._resolve(pointer), "$", found)
        return found

    def is_valid(self, instance, pointer: str = "#") -> bool:
        return not self.errors(instance, pointer)

    def _validate(self, value, schema, path: str, out: list[str]) -> None:
        if schema is True:
            return
        if schema is False:
            out.append(f"{path}: no value is allowed here")
            return
        if "$ref" in schema:
            self._validate(value, self._resolve(schema["$ref"]), path, out)
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(value, name) for name in names):
                out.append(f"{path}: expected {'/'.join(names)}, got {type(value).__name__}")
                return
        if "const" in schema and not _equal(value, schema["const"]):
            out.append(f"{path}: must equal {schema['const']!r}")
        if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
            out.append(f"{path}: must be one of {schema['enum']!r}")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                out.append(f"{path}: shorter than {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                out.append(f"{path}: longer than {schema['maxLength']}")
            if "pattern" in schema and not self._patterns[schema["pattern"]].search(value):
                out.append(f"{path}: does not match {schema['pattern']!r}")
        if _is_type(value, "number"):
            if "minimum" in schema and value < schema["minimum"]:
                out.append(f"{path}: below minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                out.append(f"{path}: above maximum {schema['maximum']}")
        if isinstance(value, dict):
            self._object(value, schema, path, out)
        if isinstance(value, list):
            self._array(value, schema, path, out)
        if "allOf" in schema:
            for child in schema["allOf"]:
                self._validate(value, child, path, out)
        if "anyOf" in schema and not any(self._passes(value, child, path) for child in schema["anyOf"]):
            out.append(f"{path}: matches none of anyOf")
        if "oneOf" in schema:
            trials = []
            for child in schema["oneOf"]:
                trial: list[str] = []
                self._validate(value, child, path, trial)
                trials.append(trial)
            matched = sum(not trial for trial in trials)
            if matched != 1:
                closest = min(trials, key=len) if matched == 0 else []
                hint = f"; closest branch: {'; '.join(closest[:2])}" if closest else ""
                out.append(f"{path}: matches {matched} of oneOf (exactly 1 required){hint}")
        if "not" in schema and self._passes(value, schema["not"], path):
            out.append(f"{path}: matches a forbidden schema")

    def _passes(self, value, schema, path: str) -> bool:
        trial: list[str] = []
        self._validate(value, schema, path, trial)
        return not trial

    def _object(self, value: dict, schema: dict, path: str, out: list[str]) -> None:
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            where = f"{path}.{key}"
            if "propertyNames" in schema and not self._passes(key, schema["propertyNames"], where):
                out.append(f"{where}: property name not allowed")
            if key in properties:
                self._validate(child, properties[key], where, out)
            elif "additionalProperties" in schema:
                self._validate(child, schema["additionalProperties"], where, out)

    def _array(self, value: list, schema: dict, path: str, out: list[str]) -> None:
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        prefix = schema.get("prefixItems", [])
        for index, item in enumerate(value):
            if index < len(prefix):
                self._validate(item, prefix[index], f"{path}[{index}]", out)
            elif "items" in schema:
                self._validate(item, schema["items"], f"{path}[{index}]", out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    # A bare definition name ("action") means "#/$defs/action"; it also survives shells that rewrite "#/...".
    pointer = argv[1] if argv[1].startswith("#") else f"#/$defs/{argv[1]}"
    try:
        validator = Validator.from_path(argv[0])
        validator._resolve(pointer)
        text = Path(argv[2]).read_text(encoding="utf-8") if argv[2] != "-" else sys.stdin.read()
        instances = [load_json(line) for line in text.splitlines() if line.strip()] if "--jsonl" in argv[3:] \
            else [load_json(text)]
    except (SchemaError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    status = 0
    for index, instance in enumerate(instances, start=1):
        problems = validator.errors(instance, pointer)
        for problem in problems:
            print(f"instance {index}: {problem}")
        status |= bool(problems)
    if not status:
        print(f"{len(instances)} instance(s) valid against {argv[1]}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
