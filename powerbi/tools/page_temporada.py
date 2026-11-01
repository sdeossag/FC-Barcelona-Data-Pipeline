"""Builds the 'Temporada' page: season KPIs, title race, table and goals."""
import json
from pathlib import Path

from pbir import (BLUE, CANVAS, INK, MUTED, RIVAL, Page, col, color,
                  container, in_filter, lit, meas, projection)

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "87106987b2f449fa8d95"

DIM = "warehouse vw_dim_competition"
RACE = "warehouse vw_title_race"
SUMMARY = "warehouse vw_team_season_summary"
MATCHES = "warehouse vw_barca_matches"

# 12-column grid on a 1280 x 720 canvas: 24 px margins, 12 px gutters.
M, G = 24, 12
W = 1280 - 2 * M


def span(cols):
    col_w = (W - 11 * G) / 12
    return round(cols * col_w + (cols - 1) * G)


def team_selector(team):
    """Selects one series of a chart by team name."""
    return {"data": [{"scopeId": {"Comparison": {
        "ComparisonKind": 0,
        "Left": col(RACE, "Equipo"),
        "Right": {"Literal": {"Value": f"'{team}'"}}}}}]}


def measure_color(name):
    return {"solid": {"color": {"expr": {"Measure": {
        "Expression": {"SourceRef": {"Entity": "Medidas"}}, "Property": name}}}}}


page = Page(REPORT / PAGE_ID)

# ---- Header: title on the left, the two global slicers on the right --------
title_runs = [
    {"textRuns": [{"value": "Temporada", "textStyle": {
        "fontFamily": "Segoe UI Semibold", "fontSize": "20pt", "color": INK}}],
     "horizontalTextAlignment": "left"},
    {"textRuns": [{"value": "FC Barcelona · resultados, puntos y clasificación", "textStyle": {
        "fontFamily": "Segoe UI", "fontSize": "10pt", "color": MUTED}}],
     "horizontalTextAlignment": "left"},
]
page.add("title", M, 14, 640, 56, {
    "visualType": "textbox",
    "objects": {"general": [{"properties": {"paragraphs": title_runs}}]},
    "visualContainerObjects": container(background=False, padding=(0, 0, 0, 0)),
})


def slicer(key, x, prop, default, group):
    return page.add(key, x, 22, 200, 36, {
        "visualType": "slicer",
        "query": {"queryState": {"Values": {"projections": [projection(col(DIM, prop), True)]}}},
        "objects": {
            "data": [{"properties": {"mode": lit("Dropdown")}}],
            "selection": [{"properties": {"singleSelect": lit(True),
                                          "selectAllCheckboxEnabled": lit(False)}}],
            "header": [{"properties": {"show": lit(False)}}],
            "items": [{"properties": {"fontSize": lit(10), "fontColor": color(INK)}}],
        },
        "visualContainerObjects": container(padding=(0, 6, 0, 6)),
        "drillFilterOtherVisuals": True,
    },
        # The saved selection is what opens the report on La Liga 2025/26.
        filters=[in_filter(DIM, prop, [default], f"f_{key}")],
        sync_group={"groupName": group, "fieldChanges": True, "filterChanges": True})


slicer("slicer_comp", 1280 - M - 200 - G - 200, "Competición", "'La Liga'", "competicion")
slicer("slicer_season", 1280 - M - 200, "Temporada", "'2025/26'", "temporada")

# ---- KPI row: four tiles, one visual each ---------------------------------
kpi_y, kpi_h = 80, 88
kpis = [
    ("Puntos", "Puntos", BLUE),
    ("Partidos jugados", "Partidos jugados", INK),
    ("Goles a favor / en contra", "Goles texto", INK),
    ("% de victorias", "% Victorias", INK),
]
kw = round((W - 3 * G) / 4)
for i, (label, measure, value_color) in enumerate(kpis):
    page.add(f"kpi{i}", M + i * (kw + G), kpi_y, kw, kpi_h, {
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

# ---- Middle row: title race (8 cols) and standings (4 cols) ---------------
mid_y, mid_h = 180, 336
race_w = span(8)
page.add("race", M, mid_y, race_w, mid_h, {
    "visualType": "lineChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(RACE, "Jornada"), True)]},
        "Series": {"projections": [projection(col(RACE, "Equipo"))]},
        "Y": {"projections": [projection(meas("Puntos acumulados"))]},
    }},
    "objects": {
        "legend": [{"properties": {"show": lit(False)}}],
        # A selector-less entry is the default for every series, so rivals stay
        # neutral grey whichever teams the season produces. Barcelona is the
        # only series named, which keeps the colour meaningful.
        "dataPoint": [
            {"properties": {"fill": color(RIVAL)}},
            {"properties": {"fill": color(BLUE)}, "selector": team_selector("FC Barcelona")},
        ],
        "lineStyles": [
            {"properties": {"strokeWidth": lit(1.5), "showMarker": lit(False)}},
            {"properties": {"strokeWidth": lit(3.5)}, "selector": team_selector("FC Barcelona")},
        ],
    },
    "visualContainerObjects": container("Carrera por el título: puntos acumulados por jornada",
                                        padding=(4, 12, 8, 8)),
}, filters=[
    in_filter(RACE, "Rival directo", ["true"], "f_rival"),
    in_filter(RACE, "Fase de liga", ["true"], "f_liga"),
])

table_x = M + race_w + G
table_w = W - race_w - G
page.add("standings", table_x, mid_y, table_w, mid_h, {
    "visualType": "tableEx",
    "query": {
        "queryState": {"Values": {"projections": [
            projection(meas("Posición")),
            projection(col(SUMMARY, "Equipo")),
            projection(col(SUMMARY, "PJ")),
            projection(col(SUMMARY, "DG")),
            projection(col(SUMMARY, "Pts")),
        ]}},
        "sortDefinition": {"sort": [{"field": col(SUMMARY, "Pts"), "direction": "Descending"}]},
    },
    "objects": {
        "grid": [{"properties": {"rowPadding": lit(2), "outlineColor": color("#EEEBE3"),
                                 "gridHorizontal": lit(True), "gridVertical": lit(False),
                                 "textSize": lit(9)}}],
        "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": lit(9),
                                          "fontFamily": lit("Segoe UI Semibold"),
                                          "backColor": color("#FFFFFF")}}],
        # Field-value formatting: a measure returns the colour, so Barcelona's
        # row is highlighted in whichever season is selected.
        "values": [
            {"properties": {"fontColorPrimary": color(INK), "fontSize": lit(9)}},
            {"properties": {"fontColor": measure_color("Color equipo"),
                            "backColor": measure_color("Fondo equipo")},
             "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                          "metadata": f"{SUMMARY}.Equipo"}},
        ],
    },
    "visualContainerObjects": container("Tabla de posiciones", padding=(4, 8, 8, 8)),
})

# ---- Bottom row: goals for and against by matchday ------------------------
bot_y = mid_y + mid_h + G
bot_h = 720 - 16 - bot_y
page.add("goals", M, bot_y, W, bot_h, {
    "visualType": "clusteredColumnChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(MATCHES, "Jornada"), True)]},
        "Y": {"projections": [projection(meas("Goles a favor")), projection(meas("Goles en contra"))]},
    }},
    "objects": {
        "legend": [{"properties": {"position": lit("TopRight"), "showTitle": lit(False),
                                   "fontSize": lit(9)}}],
        "dataPoint": [
            {"properties": {"fill": color(BLUE)}, "selector": {"metadata": "Medidas.Goles a favor"}},
            {"properties": {"fill": color(RIVAL)}, "selector": {"metadata": "Medidas.Goles en contra"}},
        ],
        "categoryAxis": [{"properties": {"fontSize": lit(9)}}],
        "valueAxis": [{"properties": {"fontSize": lit(9), "showAxisTitle": lit(False)}}],
    },
    "visualContainerObjects": container("Goles a favor y en contra por jornada",
                                        padding=(4, 12, 8, 8)),
}, filters=[in_filter(MATCHES, "Fase de liga", ["true"], "f_liga_goals")])

page.write()

# Page canvas: warm neutral so the white tiles stand out without heavy borders.
page_json = REPORT / PAGE_ID / "page.json"
doc = json.loads(page_json.read_text(encoding="utf-8"))
doc["objects"] = {"background": [{"properties": {
    "color": color(CANVAS), "transparency": lit(0)}}]}
page_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {len(page.visuals)} visuals")
