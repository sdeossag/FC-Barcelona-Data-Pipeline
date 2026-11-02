"""Builds 'En vivo y predicción': the streaming replay and the match model."""
from pathlib import Path

from layout import G, M, chart, data_table, header, kpi_row, span
from pbir import (BLUE, CANVAS, GOLD, GRANATE, INK, RIVAL, Page, col, container,
                  in_filter, meas)

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "877516657aa942dcb84f"

SCORE = "warehouse vw_live_current_score"
EVENTS = "warehouse vw_live_events_current"
FIXTURES = "warehouse vw_barca_fixtures"

page = Page(REPORT / PAGE_ID)
# The event stream shows the latest match and the fixtures are upcoming, so
# neither follows a season slicer. Leaving the slicers off avoids implying they do.
header(page, "En vivo y predicción",
       "Eventos reales de ESPN por Kafka (partido más reciente) y pronóstico del modelo",
       slicers=False)

half = span(6)
right_x = M + half + G

# ---- Left: streaming layer -------------------------------------------------
y = kpi_row(page, [
    ("Marcador", "Marcador en vivo", INK, "hero"),
    ("Rival", "Rival en vivo", INK),
    ("Eventos", "Eventos procesados", INK),
    ("Latencia (ms)", "Latencia mediana (ms)", INK),
], x0=M, total_w=half, prefix="live")

top_h = 170
chart(page, "live_shots", M, y + G, half, top_h, "Tiros y goles por equipo · partido más reciente (ESPN)",
      "clusteredColumnChart", col(SCORE, "Equipo"),
      [meas("Tiros en vivo"), meas("Goles en vivo")],
      colors={"Tiros en vivo": BLUE, "Goles en vivo": GRANATE}, legend=True, labels=True)

ev_y = y + G + top_h + G
data_table(page, "live_events", M, ev_y, half, 720 - 16 - ev_y, "Eventos recibidos por Kafka", [
    col(EVENTS, "Minuto"), col(EVENTS, "Evento"), col(EVENTS, "Jugador"),
    col(EVENTS, "Equipo"), col(EVENTS, "Detalle"),
], col(EVENTS, "Minuto"), widths=[0.1, 0.19, 0.24, 0.14, 0.33], row_padding=1)

# ---- Right: prediction layer ----------------------------------------------
kpi_row(page, [
    ("Exactitud", "Exactitud del modelo", INK, "hero"),
    ("Base", "Exactitud base", INK),
    ("Mejora", "Mejora vs base", GOLD),
    ("Log loss", "Log loss", INK),
], x0=right_x, total_w=half, prefix="model")

# "Next 45 days" keeps the chart readable; the table below lists everything.
soon = {"name": "f_soon", "field": col(FIXTURES, "Días"), "type": "Advanced", "filter": {
    "Version": 2, "From": [{"Name": "t", "Entity": FIXTURES, "Type": 0}],
    "Where": [{"Condition": {"Comparison": {"ComparisonKind": 4, "Left": {"Column": {
        "Expression": {"SourceRef": {"Source": "t"}}, "Property": "Días"}},
        "Right": {"Literal": {"Value": "45L"}}}}}]}, "howCreated": "User"}
chart(page, "probabilities", right_x, y + G, half, 250 - 0, "Probabilidad de resultado, próximos 45 días",
      "hundredPercentStackedBarChart", col(FIXTURES, "Partido corto"),
      [meas("Prob. Barça gana"), meas("Prob. empate"), meas("Prob. rival gana")],
      colors={"Prob. Barça gana": BLUE, "Prob. empate": RIVAL, "Prob. rival gana": GRANATE},
      legend=True, hide_value_axis=True, filters=[soon])

fx_y = y + G + 250 + G
data_table(page, "fixtures", right_x, fx_y, half, 720 - 16 - fx_y, "Próximos partidos y pronóstico", [
    col(FIXTURES, "Fecha"), col(FIXTURES, "Rival"), col(FIXTURES, "Sede"),
    meas("Prob. Barça gana"), meas("Prob. empate"), meas("Prob. rival gana"),
], col(FIXTURES, "Fecha"), "Ascending", widths=[0.15, 0.31, 0.1, 0.15, 0.14, 0.15], row_padding=1)

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
