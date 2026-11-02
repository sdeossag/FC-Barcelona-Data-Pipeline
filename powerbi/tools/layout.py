"""Page furniture shared by every report page: grid, header, KPI row, tables.

Keeping these here is what makes the pages look like one product: the same
margins, the same slicers in the same place, the same table styling.
"""
from pbir import (ALT, BAND, BLUE, CARD, GARNET, GOLD, INK, LINE, MUTED, col, color, container, in_filter, lit, meas,
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


def header(page, title, subtitle, slicers=True):
    """Title on the left; competition and season slicers on the right."""
    runs = [
        {"textRuns": [{"value": title, "textStyle": {
            "fontFamily": "Segoe UI Semibold", "fontSize": "20pt", "color": "#FFFFFF"}}],
         "horizontalTextAlignment": "left"},
        {"textRuns": [{"value": subtitle, "textStyle": {
            "fontFamily": "Segoe UI", "fontSize": "10pt", "color": "#C9D8EE"}}],
         "horizontalTextAlignment": "left"},
    ]
    # A Barcelona-blue band, then a strip of alternating blue and garnet blocks
    # (the shirt stripes) ending in the crest's gold. It is the one place the
    # identity is loud; the rest of the page stays quiet.
    page.add("band", 0, 0, 1280, BAND_H, {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": runs}}]},
        "visualContainerObjects": container(fill=BAND, border=BAND, radius=0,
                                            padding=(10, 0, 0, M)),
    })
    stripes = [GARNET, BAND, GARNET, BAND, GARNET, BAND, GARNET, GOLD]
    for i, stripe_color in enumerate(stripes):
        page.add(f"stripe{i}", i * 160, BAND_H, 160, 5, {
            "visualType": "textbox",
            "objects": {"general": [{"properties": {"paragraphs": [
                {"textRuns": [{"value": "", "textStyle": {"fontSize": "1pt"}}]}]}}]},
            "visualContainerObjects": container(fill=stripe_color, border=stripe_color,
                                                radius=0, padding=(0, 0, 0, 0)),
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

    if not slicers:
        return
    slicer("slicer_comp", 1280 - M - 200 - G - 200, "Competición", "'La Liga'", "competicion")
    slicer("slicer_season", 1280 - M - 200, "Temporada", "'2026/27'", "temporada")


def kpi_row(page, items, y=HEADER_BOTTOM, h=88, x0=M, total_w=W, prefix="kpi", font=26):
    """Equal tiles. items: (label, measure, value colour[, 'hero']).

    A hero tile is solid blue with white text: one per page, the number that
    answers the page's main question.
    """
    width = round((total_w - (len(items) - 1) * G) / len(items))
    for i, item in enumerate(items):
        label, measure, value_color = item[:3]
        hero = len(item) > 3
        page.add(f"{prefix}{i}", x0 + i * (width + G), y, width, h, {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [projection(meas(measure))]}}},
            "objects": {
                "labels": [{"properties": {"fontSize": lit(font), "fontFamily": lit("Segoe UI Semibold"),
                                           "color": color("#FFFFFF" if hero else value_color)}}],
                "categoryLabels": [{"properties": {"show": lit(False)}}],
                "wordWrap": [{"properties": {"show": lit(False)}}],
            },
            "visualContainerObjects": container(
                label, padding=(4, 12, 8, 12),
                **({"fill": BAND, "border": BAND, "title_color": "#C9D8EE"} if hero else {"title_color": MUTED})),
        })
    return y + h


def data_table(page, key, x, y, w, h, title, fields, sort_field, direction="Descending",
               styled=(), widths=None, row_padding=2):
    """A compact table.

    fields   fields to show; an item may be (field, display name).
    styled   (column metadata, font measure, back measure) per coloured column.
    widths   column widths as fractions of the table width. Without them a table
             hugs the left edge and leaves the tile half empty.
    The sort field must be one of the shown fields, or Power BI ignores it.
    """
    fields = [f if isinstance(f, tuple) else (f, None) for f in fields]
    values = [{"properties": {
        "fontColorPrimary": color(INK), "fontColorSecondary": color(INK),
        "backColorPrimary": color(CARD), "backColorSecondary": color(ALT),
        "fontSize": lit(10)}}]
    for metadata, font_measure, back_measure in styled:
        props = {"fontColor": measure_color(font_measure)}
        if back_measure:
            props["backColor"] = measure_color(back_measure)
        values.append({"properties": props, "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": metadata}})
    objects = {
        "grid": [{"properties": {"rowPadding": lit(row_padding), "outlineColor": color(LINE),
                                 "gridHorizontalColor": color(LINE),
                                 "gridHorizontal": lit(True), "gridVertical": lit(False),
                                 "textSize": lit(10)}}],
        "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": lit(10),
                                          "fontFamily": lit("Segoe UI Semibold"),
                                          "backColor": color(CARD)}}],
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


def axes(category_size=10, hide_value=False, value_end=None, legend=False,
         legend_position="TopRight", labels=False):
    """Axis, grid, legend and data-label colours for a dark surface."""
    value = {"fontSize": lit(9), "showAxisTitle": lit(False), "labelColor": color(MUTED),
             "gridlineShow": lit(True), "gridlineColor": color(LINE)}
    if hide_value:
        value["show"] = lit(False)
    if value_end is not None:
        value.update({"end": lit(value_end), "start": lit(0)})
    return {
        "legend": [{"properties": {"show": lit(legend), "position": lit(legend_position),
                                   "showTitle": lit(False), "fontSize": lit(9),
                                   "labelColor": color(MUTED)}}],
        "categoryAxis": [{"properties": {"fontSize": lit(category_size), "labelColor": color(MUTED),
                                         "gridlineShow": lit(False)}}],
        "valueAxis": [{"properties": value}],
        "labels": [{"properties": {"show": lit(labels), "fontSize": lit(9), "color": color(INK)}}],
    }


def chart(page, key, x, y, w, h, title, vtype, category, values, colors=None,
          filters=None, legend=False, labels=False, hide_value_axis=False,
          value_end=None, sort=None, extra=None, tooltips=()):
    """A bar/column chart with series coloured by measure.

    colors maps a measure name to a hex colour. `sort` is (field, direction).
    """
    objects = axes(hide_value=hide_value_axis, value_end=value_end, legend=legend, labels=labels)
    if colors:
        objects["dataPoint"] = [
            {"properties": {"fill": color(hex_value)}, "selector": {"metadata": f"Medidas.{name}"}}
            for name, hex_value in colors.items()]
    objects.update(extra or {})
    query = {"queryState": {
        "Category": {"projections": [projection(category, True)]},
        "Y": {"projections": [projection(v) for v in values]},
    }}
    if tooltips:
        query["queryState"]["Tooltips"] = {"projections": [projection(t) for t in tooltips]}
    if sort:
        query["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}]}
    return page.add(key, x, y, w, h, {
        "visualType": vtype, "query": query, "objects": objects,
        "visualContainerObjects": container(title, padding=(4, 12, 8, 8)),
    }, filters=filters)
