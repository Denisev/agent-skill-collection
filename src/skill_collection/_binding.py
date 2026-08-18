from __future__ import annotations

import hashlib
import json
import tomllib


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_digest(payload: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def semantic_binding_payload(binding: dict[str, object]) -> dict[str, object]:
    collection = binding["collection"]
    assert isinstance(collection, dict)
    return {
        "binding_schema_version": 1,
        "location": {"root": "project", "path": "skill-collection.toml"},
        "document": {
            "version": 1,
            "collection": {
                "url": collection["url"],
                "revision": collection["revision"],
            },
            "profile": binding["profile"],
            "target": binding.get("target", ".agents/skills"),
            "add": sorted(binding.get("add", [])),
            "remove": sorted(binding.get("remove", [])),
        },
    }


def serialize_binding(binding: dict[str, object]) -> str:
    collection = binding["collection"]
    assert isinstance(collection, dict)
    rendered = (
        f'version = 1\nprofile = {toml_string(str(binding["profile"]))}\n'
        'target = ".agents/skills"\n\n[collection]\n'
        f'url = {toml_string(str(collection["url"]))}\n'
        f'revision = {toml_string(str(collection["revision"]))}\n'
    )
    parsed = tomllib.loads(rendered)
    if parsed != binding:
        raise ValueError("Binding strings must round-trip as canonical TOML.")
    return rendered
