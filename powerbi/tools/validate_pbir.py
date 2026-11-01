"""Validates every visual.json of a page against Microsoft's published schema.

Usage: python validate_pbir.py <page folder>
Fetches the schemas once and caches them next to this script.
"""
import json
import sys
import urllib.request
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

CACHE = Path(__file__).parent / ".schema_cache"
CACHE.mkdir(exist_ok=True)


def retrieve(uri):
    cached = CACHE / (uri.replace("://", "_").replace("/", "_"))
    if not cached.exists():
        with urllib.request.urlopen(uri, timeout=30) as resp:
            cached.write_bytes(resp.read())
    return Resource.from_contents(json.loads(cached.read_text(encoding="utf-8")))


registry = Registry(retrieve=retrieve)
failures = 0
for path in sorted(Path(sys.argv[1]).rglob("*.json")):
    doc = json.loads(path.read_text(encoding="utf-8"))
    schema_uri = doc.get("$schema")
    if not schema_uri:
        continue
    validator = jsonschema.Draft7Validator({"$ref": schema_uri}, registry=registry)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    status = "ok" if not errors else f"{len(errors)} error(s)"
    print(f"{status:>12}  {path.relative_to(sys.argv[1])}")
    for err in errors[:5]:
        failures += 1
        print("             ", "/".join(map(str, err.path)), "->", err.message[:160])

sys.exit(1 if failures else 0)
