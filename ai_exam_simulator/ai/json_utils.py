"""Robust extraction of JSON objects from model output."""

from __future__ import annotations

import json
import re
from typing import Any

from .provider_interface import MalformedResponseError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in ``text`` (handles code fences and chatter)."""
    if not text or not text.strip():
        raise MalformedResponseError("The model returned an empty response.")
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # scan for a balanced {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(opener, start + 1)
    raise MalformedResponseError("The model's response was not valid JSON.")


def as_object(data: Any, key: str | None = None) -> dict:
    if isinstance(data, dict):
        return data
    if key and isinstance(data, list):
        return {key: data}
    raise MalformedResponseError("Expected a JSON object from the model.")
