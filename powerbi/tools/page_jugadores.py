"""Builds the 'Jugadores' page: top scorers, expected goals and finishing."""
from pathlib import Path

from layout import G, M, W, axes, chart, data_table, header, kpi_row, span
from pbir import (BLUE, CANVAS, INK, RIVAL, Page, col, color, container, lit, meas,
                  projection)

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "959b262bc06b40d4adab"

SCORERS = "warehouse vw_top_scorers"
SHOTS = "warehouse vw_player_shot_quality"

page = Page(REPORT / PAGE_ID)
header(page, "Jugadores", "Goleadores de la competición y calidad de finalización")
y = kpi_row(page, [
    ("Goleador del Barça", "Goleador del Barça", INK, "hero"),
    ("Goles del goleador", "Goles del goleador", INK),
    ("Goles del Barça en el top 10", "Goles del Barça en el top 10", INK),
    ("Máximo goleador de la competición", "Máximo goleador", INK),
])

# ---- Middle row: scorers as bars (Barcelona in blue) and as a table --------
mid_y, mid_h = y + G, 250
bars_w = span(5)
# Two series instead of one coloured by a measure: Barcelona's players are blue
# and everyone else grey, which Power BI applies reliably. The total goals sit in
# the tooltip so the bars can still be sorted by it.
chart(page, "scorers_bars", M, mid_y, bars_w, mid_h, "Máximos goleadores", "barChart",
      col(SCORERS, "Jugador"), [meas("Goles (Barça)"), meas("Goles (resto)")],
      colors={"Goles (Barça)": BLUE, "Goles (resto)": RIVAL}, labels=True, hide_value_axis=True,
      tooltips=[meas("Goles jugador")], sort=(meas("Goles jugador"), "Descending"))

data_table(page, "scorers_table", M + bars_w + G, mid_y, W - bars_w - G, mid_h,
           "Tabla de goleadores", [
    col(SCORERS, "Jugador"), col(SCORERS, "Equipo"), col(SCORERS, "Goles"),
    col(SCORERS, "Asistencias"), col(SCORERS, "G+A"), col(SCORERS, "Sin penalti"),
], col(SCORERS, "Goles"), widths=[0.3, 0.26, 0.1, 0.12, 0.1, 0.12], row_padding=1,
    styled=[(f"{SCORERS}.{c}", "Texto jugador", "Fondo jugador")
            for c in ("Jugador", "Equipo", "Goles", "Asistencias", "G+A", "Sin penalti")])

# ---- Bottom row: expected goals against goals, and finishing efficiency ----
low_y = mid_y + mid_h + G
low_h = 720 - 16 - low_y
half = span(6)
page.add("xg_scatter", M, low_y, half, low_h, {
    "visualType": "scatterChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(SHOTS, "Jugador"), True)]},
        "X": {"projections": [projection(meas("xG acumulado"))]},
        "Y": {"projections": [projection(meas("Goles reales"))]},
    }},
    "objects": {
        **axes(),
        "dataPoint": [{"properties": {"fill": color(BLUE)}}],
        "categoryLabels": [{"properties": {"show": lit(True), "fontSize": lit(9),
                                           "color": color(INK)}}],
    },
    "visualContainerObjects": container("xG contra goles · StatsBomb, Barça 4-0 Granada (2020/21)",
                                        padding=(4, 12, 8, 8)),
})

data_table(page, "efficiency", M + half + G, low_y, W - half - G, low_h,
           "Eficiencia de tiro por jugador", [
    col(SHOTS, "Jugador"), col(SHOTS, "Equipo"), col(SHOTS, "Tiros"),
    (col(SHOTS, "Goles anotados"), "Goles"), col(SHOTS, "xG"),
    col(SHOTS, "Diferencia xG"),
], col(SHOTS, "xG"), widths=[0.32, 0.2, 0.1, 0.1, 0.12, 0.16], row_padding=1)

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
