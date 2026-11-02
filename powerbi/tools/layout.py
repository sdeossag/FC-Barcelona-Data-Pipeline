"""Page furniture shared by every report page: grid, header, KPI row, tables.

Keeping these here is what makes the pages look like one product: the same
margins, the same slicers in the same place, the same table styling.
"""
from pbir import (BLUE, INK, MUTED, col, color, container, in_filter, lit, meas,
                  projection)

DIM = "warehouse vw_dim_competition"

# 12-column grid on a 1280 x 720 canvas: 24 px margins, 12 px gutters.
M, G = 24, 12
W = 1280 - 2 * M
HEADER_BOTTOM = 80


def span(cols):
    """Width in pixels of `cols` grid columns, gutters included."""
    col_w = (W - 11 * G) / 12
    return round(cols * col_w + (cols - 1) * G)


def measure_color(name):
    """A colour that is the result of a measure (field-value formatting)."""
    return {"solid": {"color": {"expr": {"Measure": {
        "Expression": {"SourceRef": {"Entity": "Medidas"}}, "Property": name}}}}}


def header(page, title, subtitle):
    """Title on the left; competition and season slicers on the right."""
    runs = [
        {"textRuns": [{"value": title, "textStyle": {
            "fontFamily": "Segoe UI Semibold", "fontSize": "20pt", "color": INK}}],
         "horizontalTextAlignment": "left"},
        {"textRuns": [{"value": subtitle, "textStyle": {
            "fontFamily": "Segoe UI", "fontSize": "10pt", "color": MUTED}}],
         "horizontalTextAlignment": "left"},
    ]
    page.add("title", M, 14, 640, 56, {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": runs}}]},
        "visualContainerObjects": container(background=False, padding=(0, 0, 0, 0)),
    })

    def slicer(key, x, prop, default, group):
        page.add(key, x, 22, 200, 36, {
            "visualType": "slicer",
            "query": {"queryState": {"Values": {"projections": [projection(col(DIM, prop), True)]}}},
            "objects": {
                # A slicer keeps its selection in general.filter. A filterConfig
                # filter would instead filter the slicer's own list.
                "general": [{"properties": {"filter": {"filter": in_filter(
                    DIM, prop, [default], "sel")["filter"]}}}],
                "data": [{"properties": {"mode": lit("Dropdown")}}],
                "selection": [{"properties": {"singleSelect": lit(True),
                                              "selectAllCheckboxEnabled": lit(False)}}],
                "header": [{"properties": {"show": lit(False)}}],
                "items": [{"properties": {"fontSize": lit(10), "fontColor": color(INK)}}],
            },
            "visualContainerObjects": container(padding=(0, 6, 0, 6)),
            "drillFilterOtherVisuals": True,
        },
            sync_group={"groupName": group, "fieldChanges": True, "filterChanges": True})

    slicer("slicer_comp", 1280 - M - 200 - G - 200, "Competición", "'La Liga'", "competicion")
    slicer("slicer_season", 1280 - M - 200, "Temporada", "'2025/26'", "temporada")


def kpi_row(page, items, y=HEADER_BOTTOM, h=88):
    """Four equal tiles. items: (label, measure name, value colour)."""
    width = round((W - 3 * G) / len(items) if len(items) == 4 else (W - (len(items) - 1) * G) / len(items))
    for i, (label, measure, value_color) in enumerate(items):
        page.add(f"kpi{i}", M + i * (width + G), y, width, h, {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [projection(meas(measure))]}}},
            "objects": {
                "labels": [{"properties": {"fontSize": lit(26), "fontFamily": lit("Segoe UI Semibold"),
                                           "color": color(value_color)}}],
                "categoryLabels": [{"properties": {"show": lit(False)}}],
                "wordWrap": [{"properties": {"show": lit(False)}}],
            },
            "visualContainerObjects": container(label, padding=(4, 12, 8, 12)),
        })
    return y + h


def data_table(page, key, x, y, w, h, title, fields, sort_field, direction="Descending",
               styled=(), widths=None):
    """A compact table.

    fields   fields to show; an item may be (field, display name).
    styled   (column metadata, font measure, back measure) per coloured column.
    widths   column widths as fractions of the table width. Without them a table
             hugs the left edge and leaves the tile half empty.
    The sort field must be one of the shown fields, or Power BI ignores it.
    """
    fields = [f if isinstance(f, tuple) else (f, None) for f in fields]
    values = [{"properties": {"fontColorPrimary": color(INK), "fontSize": lit(10)}}]
    for metadata, font_measure, back_measure in styled:
        props = {"fontColor": measure_color(font_measure)}
        if back_measure:
            props["backColor"] = measure_color(back_measure)
        values.append({"properties": props, "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": metadata}})
    objects = {
        "grid": [{"properties": {"rowPadding": lit(2), "outlineColor": color("#EEEBE3"),
                                 "gridHorizontal": lit(True), "gridVertical": lit(False),
                                 "textSize": lit(10)}}],
        "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": lit(10),
                                          "fontFamily": lit("Segoe UI Semibold"),
                                          "backColor": color("#FFFFFF")}}],
        "values": values,
        # A total row adds nothing to a list of matches or teams.
        "total": [{"properties": {"totals": lit(False)}}],
    }
    if widths:
        usable = w - 24
        objects["columnWidth"] = [
            {"properties": {"value": lit(round(usable * frac))},
             "selector": {"metadata": projection(f)["queryRef"]}}
            for (f, _), frac in zip(fields, widths)]
    return page.add(key, x, y, w, h, {
        "visualType": "tableEx",
        "query": {
            "queryState": {"Values": {"projections": [projection(f, display=d) for f, d in fields]}},
            "sortDefinition": {"sort": [{"field": sort_field, "direction": direction}]},
        },
        "objects": objects,
        "visualContainerObjects": container(title, padding=(4, 8, 8, 8)),
    })
