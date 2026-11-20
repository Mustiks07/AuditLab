"""Bounded, recursion-free intake of untrusted actions (Issue #23).

Every action handed to the runner is walked once, iteratively, before anything else touches it.
The walk returns an independent JSON-safe copy, or the reason the value cannot be accepted:

* ``not_json``    — a value the JSON wire format cannot carry: anything except exactly dict
                    (string keys), list, str, int, float, bool and None (subclasses are refused
                    too); NaN/Infinity; integers longer than the runner's JSON codec accepts
                    (about 4300 decimal digits). This is a damaged packet, never a submission.
* ``depth_limit`` — valid JSON nested deeper than MAX_ACTION_DEPTH levels (the action object is
                    level 1, as in schemas/validate.py). No valid action is deeper than 6.
* ``size_limit``  — valid JSON with more than MAX_ACTION_SIZE nodes plus string characters.

Nothing here recurses, so hostile nesting cannot overflow the interpreter stack.
"""
from __future__ import annotations

import math

MAX_ACTION_DEPTH = 64
MAX_ACTION_SIZE = 1_048_576
# CPython refuses int<->str conversion beyond 4300 digits; 14284 bits is the largest safe magnitude.
MAX_INT_BITS = 14_284


def json_snapshot(value: object, max_depth: int = MAX_ACTION_DEPTH,
                  max_size: int = MAX_ACTION_SIZE) -> tuple[object | None, str | None]:
    """Return (copy, None) for an acceptable JSON value, else (None, reason)."""
    holder: list = [None]
    stack = [(value, holder, 0, 1)]  # (source, parent container, key/index, depth)
    size = 0
    while stack:
        source, parent, slot, depth = stack.pop()
        if depth > max_depth:
            return None, "depth_limit"
        size += 1
        kind = type(source)
        if source is None or kind is bool:
            parent[slot] = source
        elif kind is str:
            size += len(source)
            parent[slot] = source
        elif kind is int:
            if source.bit_length() > MAX_INT_BITS:
                return None, "not_json"
            parent[slot] = source
        elif kind is float:
            if not math.isfinite(source):
                return None, "not_json"
            parent[slot] = source
        elif kind is list:
            if size + len(source) > max_size:  # refuse before expanding a huge container
                return None, "size_limit"
            copy: list = [None] * len(source)
            parent[slot] = copy
            for index in range(len(source) - 1, -1, -1):
                stack.append((source[index], copy, index, depth + 1))
        elif kind is dict:
            if size + len(source) > max_size:
                return None, "size_limit"
            copy_dict: dict = {}
            parent[slot] = copy_dict
            items = list(source.items())
            for key, _ in items:
                if type(key) is not str:
                    return None, "not_json"
                size += len(key)
                copy_dict[key] = None  # keep key order of the source
            for key, child in reversed(items):
                stack.append((child, copy_dict, key, depth + 1))
        else:
            return None, "not_json"
        if size > max_size:
            return None, "size_limit"
    return holder[0], None


def rejected_marker(reason: str) -> dict:
    """What the trajectory and trusted record keep instead of an unacceptable argument value."""
    return {"$unrepresentable": reason}
