"""Builds the 'Partidos' page: results, recent form and home against away."""
from pathlib import Path

from layout import G, M, W, data_table, header, kpi_row, span
from pbir import (BLUE, CANVAS, GRANATE, INK, Page, col, color, container, lit, meas,
                  projection)

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "bebb052d48ba4e3eaa0b"

MATCHES = "warehouse vw_barca_matches"
FORM = "warehouse vw_barca_form_last5"
HOME_AWAY = "warehouse vw_barca_home_away"

page = Page(REPORT / PAGE_ID)
header(page, "Partidos", "Resultados, local contra visitante y forma reciente")
y = kpi_row(page, [
    ("Victorias", "Victorias", INK, "hero"),
    ("Empates", "Empates", INK),
    ("Derrotas", "Derrotas", GRANATE),
    ("Diferencia de goles", "Diferencia de goles", BLUE),
])

# ---- Middle row: recent form, home/away table and win-rate bars -----------
mid_y, mid_h = y + G, 212
form_w = span(5)
data_table(page, "form", M, mid_y, form_w, mid_h, "Forma reciente: últimos 5", [
    col(FORM, "Fecha"), col(FORM, "Resultado"), col(FORM, "Rival"), col(FORM, "Sede"),
    col(FORM, "Marcador"),
], col(FORM, "Fecha"), "Descending", widths=[0.22, 0.18, 0.3, 0.12, 0.18],
    styled=[(f"{FORM}.Resultado", "Color forma", None)])

split_w = span(4)
data_table(page, "home_away", M + form_w + G, mid_y, split_w, mid_h, "Casa contra fuera", [
    col(HOME_AWAY, "Sede"), col(HOME_AWAY, "PJ"), col(HOME_AWAY, "% Victorias"),
    col(HOME_AWAY, "GF/partido"), col(HOME_AWAY, "GC/partido"),
], col(HOME_AWAY, "Sede"), "Ascending", widths=[0.2, 0.12, 0.24, 0.22, 0.22])

bars_x = M + form_w + G + split_w + G
page.add("win_rate", bars_x, mid_y, W - (bars_x - M), mid_h, {
    "visualType": "clusteredBarChart",
    "query": {"queryState": {
        "Category": {"projections": [projection(col(MATCHES, "Sede"), True)]},
        "Y": {"projections": [projection(meas("% Victorias"))]},
    }},
    "objects": {
        "legend": [{"properties": {"show": lit(False)}}],
        "dataPoint": [{"properties": {"fill": color(BLUE)}}],
        "labels": [{"properties": {"show": lit(True), "fontSize": lit(9)}}],
        "valueAxis": [{"properties": {"show": lit(False), "end": lit(1), "start": lit(0)}}],
        "categoryAxis": [{"properties": {"fontSize": lit(9)}}],
    },
    "visualContainerObjects": container("% de victorias por sede", padding=(4, 12, 8, 8)),
})

# ---- Bottom: every result of the selected season --------------------------
res_y = mid_y + mid_h + G
data_table(page, "results", M, res_y, W, 720 - 16 - res_y, "Resultados de la temporada", [
    col(MATCHES, "Fecha"), col(MATCHES, "Jornada"), col(MATCHES, "Rival"),
    col(MATCHES, "Sede"), col(MATCHES, "Marcador"), col(MATCHES, "Resultado"),
], col(MATCHES, "Fecha"), widths=[0.12, 0.09, 0.3, 0.12, 0.14, 0.23],
    styled=[(f"{MATCHES}.Resultado", "Color resultado", None)])

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
