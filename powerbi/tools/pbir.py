"""Small helpers that write PBIR visual containers as plain JSON.

PBIR stores every visual as one JSON file, which makes a report reviewable and
repeatable from code. These helpers hide the verbose "expr/Literal" wrappers so
a page script can describe a layout in a few lines.
"""
import hashlib
import json
import shutil
from pathlib import Path

SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/item/report/"
          "definition/visualContainer/2.8.0/schema.json")

# Design tokens. Barca colours are accents; everything else is neutral.
INK = "#16181D"
MUTED = "#6D7178"
CARD = "#FFFFFF"
LINE = "#E6E2D9"
CANVAS = "#F5F3EE"
BLUE = "#004D98"
GRANATE = "#A50044"
RIVAL = "#B4B2A9"


def lit(value):
    """A literal expression: strings are quoted, numbers get the D suffix."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        value = f"{value}D"
    else:
        value = f"'{value}'"
    return {"expr": {"Literal": {"Value": value}}}


def color(hex_value):
    return {"solid": {"color": lit(hex_value)}}


def col(table, prop):
    return {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": prop}}


def meas(prop, table="Medidas"):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": prop}}


def projection(field, active=False, display=None):
    kind = "Measure" if "Measure" in field else "Column"
    inner = field[kind]
    entity = inner["Expression"]["SourceRef"]["Entity"]
    prop = inner["Property"]
    out = {"field": field, "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop}
    if active:
        out["active"] = True
    if display:
        out["displayName"] = display
    return out


def in_filter(table, prop, values, name):
    """A categorical 'is one of' filter, the shape Power BI writes itself."""
    return {
        "name": name,
        "field": col(table, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": table, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": prop}}],
                "Values": [[{"Literal": {"Value": v}}] for v in values],
            }}}],
        },
        "howCreated": "User",
    }


def container(title=None, background=True, padding=None, fill=CARD, title_color=INK,
              border=LINE, radius=8):
    """Card chrome shared by every visual: white tile, hairline, 8 px radius.

    `fill` and `title_color` turn a tile into a solid hero tile; `radius` 0 gives
    the square corners a full-width band needs.
    """
    objs = {
        "title": [{"properties": {"show": lit(bool(title)), **({
            "text": lit(title), "fontColor": color(title_color), "fontSize": lit(11),
            "fontFamily": lit("Segoe UI Semibold"), "alignment": lit("left"),
        } if title else {})}}],
        "background": [{"properties": {"show": lit(background), "color": color(fill),
                                       "transparency": lit(0)}}],
        "border": [{"properties": {"show": lit(background), "color": color(border),
                                   "radius": lit(radius), "width": lit(1)}}],
    }
    if padding is not None:
        t, r, b, l = padding
        objs["padding"] = [{"properties": {"top": lit(t), "right": lit(r),
                                           "bottom": lit(b), "left": lit(l)}}]
    return objs


class Page:
    def __init__(self, page_dir):
        self.dir = Path(page_dir)
        self.visuals = []

    def add(self, key, x, y, w, h, visual, filters=None, sync_group=None):
        name = hashlib.md5(f"{self.dir.name}/{key}".encode()).hexdigest()[:20]
        z = len(self.visuals) * 10
        doc = {
            "$schema": SCHEMA,
            "name": name,
            "position": {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": z},
            "visual": visual,
        }
        if filters:
            doc["filterConfig"] = {"filters": filters}
        if sync_group:
            # The schema places the sync group inside the visual, not the container.
            visual["syncGroup"] = sync_group
        self.visuals.append(doc)
        return name

    def set_canvas(self, hex_value):
        """Page background: warm neutral so white tiles stand out without heavy borders."""
        page_json = self.dir / "page.json"
        doc = json.loads(page_json.read_text(encoding="utf-8"))
        doc["objects"] = {"background": [{"properties": {
            "color": color(hex_value), "transparency": lit(0)}}]}
        page_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    def write(self):
        """Replace the page's visuals folder so no stale shape survives."""
        target = self.dir / "visuals"
        if target.exists():
            shutil.rmtree(target)
        for doc in self.visuals:
            d = target / doc["name"]
            d.mkdir(parents=True)
            (d / "visual.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
