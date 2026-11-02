"""Builds the 'Jugadores' page: top scorers, goals by stretch of the match and finishing."""
from pathlib import Path

from layout import G, M, W, chart, data_table, header, kpi_row, span
from pbir import BLUE, CANVAS, GRANATE, INK, RIVAL, Page, col, meas

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "959b262bc06b40d4adab"

SCORERS = "warehouse vw_top_scorers"
EVENTS = "warehouse vw_barca_player_events"
INTERVALS = "warehouse vw_barca_goals_by_interval"

page = Page(REPORT / PAGE_ID)
header(page, "Jugadores", "Goleadores de la competición, goles por tramo y remate de la plantilla")
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

# ---- Bottom row: when Barcelona scores, and who finishes and gets booked ---
# Both come from the ESPN event stream of this season's matches, so they are not
# filtered by the competition and season slicers.
low_y = mid_y + mid_h + G
low_h = 720 - 16 - low_y
half = span(5)
chart(page, "goal_intervals", M, low_y, half, low_h,
      "Goles por tramo del partido · temporada 2026/27 (ESPN)", "clusteredColumnChart",
      col(INTERVALS, "Tramo"),
      [meas("Goles a favor por tramo"), meas("Goles en contra por tramo")],
      colors={"Goles a favor por tramo": BLUE, "Goles en contra por tramo": GRANATE},
      legend=True, labels=True)

data_table(page, "player_events", M + half + G, low_y, W - half - G, low_h,
           "Remate y disciplina del Barça · temporada 2026/27 (ESPN)", [
    col(EVENTS, "Jugador"), col(EVENTS, "Goles"), col(EVENTS, "Asistencias"),
    col(EVENTS, "Tiros"), col(EVENTS, "A puerta"), col(EVENTS, "% a puerta"),
    col(EVENTS, "Amarillas"), col(EVENTS, "Rojas"),
], col(EVENTS, "Goles"), widths=[0.26, 0.09, 0.12, 0.09, 0.1, 0.12, 0.12, 0.1], row_padding=1)

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
