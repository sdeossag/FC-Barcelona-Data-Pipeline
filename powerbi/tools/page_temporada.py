"""Builds the 'Temporada' page: season KPIs, title race, table and goals."""
import json
from pathlib import Path

from layout import G, M, W, axes, data_table, header, kpi_row, span
from pbir import (BLUE, CANVAS, GOLD, GRANATE, INK, RIVAL, Page, col, color, container,
                  in_filter, lit, meas, projection)

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "87106987b2f449fa8d95"

RACE = "warehouse vw_title_race"
SUMMARY = "warehouse vw_team_season_summary"
MATCHES = "warehouse vw_barca_matches"


# Clubs that have appeared among the six best sides of a selected season.
RIVALS = [
    "Arsenal FC", "Aston Villa FC", "Athletic Club", "Bayer 04 Leverkusen", "Chelsea FC",
    "Club Atlético de Madrid", "FC Bayern München", "FC Internazionale Milano", "Girona FC",
    "Liverpool FC", "Paris Saint-Germain FC", "RC Celta de Vigo", "Real Betis Balompié",
    "Real Madrid CF", "Real Sociedad de Fútbol", "Sevilla FC", "Tottenham Hotspur FC",
    "VfB Stuttgart", "Villarreal CF",
]


def team_selector(team):
    """Selects one series of a chart by team name."""
    return {"data": [{"scopeId": {"Comparison": {
        "ComparisonKind": 0,
        "Left": col(RACE, "Equipo"),
        "Right": {"Literal": {"Value": f"'{team}'"}}}}}]}


page = Page(REPORT / PAGE_ID)
header(page, "Temporada", "FC Barcelona · resultados, puntos y clasificación")
y = kpi_row(page, [
    ("Puntos", "Puntos", BLUE, "hero"),
    ("Partidos jugados", "Partidos jugados", INK),
    ("Goles a favor / en contra", "Goles texto", INK),
    ("% de victorias", "% Victorias", GOLD),
])

# ---- Middle row: title race (8 cols) and standings (4 cols) ---------------
mid_y, mid_h = y + G, 336
race_w = span(8)
page.add("race", M, mid_y, race_w, mid_h, {
    "visualType": "lineChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(RACE, "Jornada"), True)]},
        "Series": {"projections": [projection(col(RACE, "Equipo"))]},
        "Y": {"projections": [projection(meas("Puntos acumulados"))]},
    }},
    "objects": {
        **axes(),
        # A selector-less entry is the default for every series, so rivals stay
        # neutral grey whichever teams the season produces. Barcelona is the
        # only series named, which keeps the colour meaningful.
        # Power BI has no default series colour, so every club that has been a
        # direct rival gets the neutral grey by name. Barcelona is blue.
        "dataPoint": [
            *[{"properties": {"fill": color(RIVAL)}, "selector": team_selector(t)} for t in RIVALS],
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
    in_filter(RACE, "Visible", ["true"], "f_rival"),
    in_filter(RACE, "Fase de liga", ["true"], "f_liga"),
])

# The standings run the full height of the right column so all 20 clubs fit
# without scrolling. Rows come from measures, not from the PJ/DG/Pts columns:
# grouping by those columns made rows from other seasons appear.
data_table(page, "standings", M + race_w + G, mid_y, W - race_w - G, 720 - 16 - mid_y,
           "Tabla de posiciones", [
    (meas("Posición"), "#"), col(SUMMARY, "Equipo"), (meas("PJ tabla"), "PJ"),
    (meas("DG tabla"), "DG"), (meas("Puntos tabla"), "Pts"),
], meas("Posición"), "Ascending", widths=[0.09, 0.5, 0.11, 0.15, 0.15], row_padding=0,
    # Highlight Barcelona's whole row, not just the name cell.
    styled=[(ref, "Color equipo", "Fondo equipo") for ref in (
        "Medidas.Posición", f"{SUMMARY}.Equipo", "Medidas.PJ tabla",
        "Medidas.DG tabla", "Medidas.Puntos tabla")])

# ---- Bottom left: goals for and against by matchday -----------------------
bot_y = mid_y + mid_h + G
page.add("goals", M, bot_y, race_w, 720 - 16 - bot_y, {
    "visualType": "clusteredColumnChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(MATCHES, "Jornada"), True)]},
        "Y": {"projections": [projection(meas("Goles a favor")), projection(meas("Goles en contra"))]},
    }},
    "objects": {
        **axes(legend=True),
        "dataPoint": [
            {"properties": {"fill": color(BLUE)}, "selector": {"metadata": "Medidas.Goles a favor"}},
            {"properties": {"fill": color(GRANATE)}, "selector": {"metadata": "Medidas.Goles en contra"}},
        ],
    },
    "visualContainerObjects": container("Goles a favor y en contra por jornada",
                                        padding=(4, 12, 8, 8)),
}, filters=[in_filter(MATCHES, "Fase de liga", ["true"], "f_liga_goals")])

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
