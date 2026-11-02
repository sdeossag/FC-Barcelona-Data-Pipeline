"""Page furniture shared by every report page: grid, header, KPI row, tables.

Keeping these here is what makes the pages look like one product: the same
margins, the same slicers in the same place, the same table styling.
"""
from pbir import (BLUE, GRANATE, INK, MUTED, col, color, container, in_filter, lit, meas,
                  projection)

DIM = "warehouse vw_dim_competition"

# 12-column grid on a 1280 x 720 canvas: 24 px margins, 12 px gutters.
M, G = 24, 12
W = 1280 - 2 * M
BAND_H = 72
HEADER_BOTTOM = BAND_H + 16


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
            "fontFamily": "Segoe UI Semibold", "fontSize": "20pt", "color": "#FFFFFF"}}],
         "horizontalTextAlignment": "left"},
        {"textRuns": [{"value": subtitle, "textStyle": {
            "fontFamily": "Segoe UI", "fontSize": "10pt", "color": "#C9D8EE"}}],
         "horizontalTextAlignment": "left"},
    ]
    # A solid Barcelona-blue band with a thin garnet stripe under it. It is the
    # one place the identity is loud; the rest of the page stays quiet.
    page.add("band", 0, 0, 1280, BAND_H, {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": runs}}]},
        "visualContainerObjects": container(fill=BLUE, border=BLUE, radius=0,
                                            padding=(10, 0, 0, M)),
    })
    page.add("stripe", 0, BAND_H, 1280, 4, {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": [
            {"textRuns": [{"value": " ", "textStyle": {"fontSize": "1pt"}}]}]}}]},
        "visualContainerObjects": container(fill=GRANATE, border=GRANATE, radius=0,
                                            padding=(0, 0, 0, 0)),
    })

    def slicer(key, x, prop, default, group):
        page.add(key, x, 18, 200, 36, {
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
    """Equal tiles. items: (label, measure, value colour[, 'hero']).

    A hero tile is solid blue with white text: one per page, the number that
    answers the page's main question.
    """
    width = round((W - (len(items) - 1) * G) / len(items))
    for i, item in enumerate(items):
        label, measure, value_color = item[:3]
        hero = len(item) > 3
        page.add(f"kpi{i}", M + i * (width + G), y, width, h, {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [projection(meas(measure))]}}},
            "objects": {
                "labels": [{"properties": {"fontSize": lit(26), "fontFamily": lit("Segoe UI Semibold"),
                                           "color": color("#FFFFFF" if hero else value_color)}}],
                "categoryLabels": [{"properties": {"show": lit(False)}}],
                "wordWrap": [{"properties": {"show": lit(False)}}],
            },
            "visualContainerObjects": container(
                label, padding=(4, 12, 8, 12),
                **({"fill": BLUE, "border": BLUE, "title_color": "#FFFFFF"} if hero else {})),
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
        usable = w - 44  # padding plus the vertical scrollbar
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
