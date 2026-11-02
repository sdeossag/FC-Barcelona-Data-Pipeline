"""Checks that every table.field a report visual references exists in the model.

The JSON schema cannot catch a misspelt column name; Power BI only shows an
empty visual. This catches it before the file is opened.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "barca_dashboard.SemanticModel" / "definition" / "tables"
PAGES = ROOT / "barca_dashboard.Report" / "definition" / "pages"


def unquote(name):
    name = name.strip()
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


known = {}
for tmdl in MODEL.glob("*.tmdl"):
    text = tmdl.read_text(encoding="utf-8")
    table = unquote(re.search(r"^table (.+)$", text, re.M).group(1))
    names = set()
    for m in re.finditer(r"^\t(?:column|measure) ('(?:[^']|'')*'|[^\s=]+)", text, re.M):
        names.add(unquote(m.group(1)))
    known[table] = names

missing = set()


def walk(node, where):
    if isinstance(node, dict):
        for kind in ("Column", "Measure"):
            inner = node.get(kind)
            if isinstance(inner, dict) and "Property" in inner:
                src = inner.get("Expression", {}).get("SourceRef", {})
                entity = src.get("Entity")
                if entity and inner["Property"] not in known.get(entity, ()):
                    missing.add((where, entity, inner["Property"]))
        for key, value in node.items():
            if key == "metadata" and isinstance(value, str) and "." in value:
                entity, prop = value.split(".", 1)
                if entity in known and prop not in known[entity]:
                    missing.add((where, entity, prop))
            walk(value, where)
    elif isinstance(node, list):
        for item in node:
            walk(item, where)


for path in PAGES.rglob("visual.json"):
    walk(json.loads(path.read_text(encoding="utf-8")), path.parent.parent.parent.name)

for where, entity, prop in sorted(missing):
    print(f"MISSING  page {where}: {entity}[{prop}]")
print("all fields resolve" if not missing else f"{len(missing)} unresolved reference(s)")
sys.exit(1 if missing else 0)
