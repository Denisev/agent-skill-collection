from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from typing import Any


def json_document(command: str, result: object) -> str:
    payload = {
        "command": command,
        "result": _value(result),
        "schema_version": 1,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def error_document(code: str, message: str) -> str:
    return json.dumps(
        {"error": {"code": code, "message": message}, "schema_version": 1},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _value(value: object) -> Any:
    if is_dataclass(value):
        return _value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value
