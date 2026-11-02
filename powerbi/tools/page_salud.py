"""Builds 'Salud del pipeline': load freshness, row counts and quality checks."""
from pathlib import Path

from layout import G, M, W, chart, data_table, header, kpi_row, span
from pbir import BLUE, CANVAS, INK, Page, col, meas

REPORT = Path(__file__).resolve().parent.parent / "barca_dashboard.Report" / "definition" / "pages"
PAGE_ID = "7de63a0779e247f7a32d"

HEALTH = "warehouse vw_pipeline_health"
CHECKS = "warehouse vw_quality_results_latest"

page = Page(REPORT / PAGE_ID)
# Health describes the warehouse as a whole, so no season slicers.
header(page, "Salud del pipeline",
       "Frescura de las cargas, volumen por tabla y controles de calidad de la última corrida",
       slicers=False)
y = kpi_row(page, [
    ("Tasa de aprobación de controles", "Tasa de aprobación", INK, "hero"),
    ("Controles superados", "Controles texto", INK),
    ("Tablas frescas", "Tablas frescas texto", INK),
    ("Filas en el warehouse", "Filas en warehouse", INK),
])

mid_y, mid_h = y + G, 250
table_w = span(7)
data_table(page, "freshness", M, mid_y, table_w, mid_h, "Frescura por tabla", [
    col(HEALTH, "Tabla"), col(HEALTH, "Origen"), col(HEALTH, "Filas"),
    (col(HEALTH, "Horas"), "Horas desde la carga"), col(HEALTH, "Estado"),
], col(HEALTH, "Horas"), "Ascending", widths=[0.24, 0.24, 0.14, 0.2, 0.18], row_padding=1,
    styled=[(f"{HEALTH}.Estado", "Color estado", None)])

chart(page, "rows", M + table_w + G, mid_y, W - table_w - G, mid_h, "Filas por tabla",
      "clusteredBarChart", col(HEALTH, "Tabla"), [meas("Filas en warehouse")],
      colors={"Filas en warehouse": BLUE}, labels=True, hide_value_axis=True,
      sort=(meas("Filas en warehouse"), "Descending"))

low_y = mid_y + mid_h + G
data_table(page, "checks", M, low_y, W, 720 - 16 - low_y, "Controles de calidad de la última corrida", [
    col(CHECKS, "Control"), col(CHECKS, "Esperado"), col(CHECKS, "Valor"),
    col(CHECKS, "Estado"), col(CHECKS, "Fecha"),
], col(CHECKS, "Control"), "Ascending", widths=[0.26, 0.4, 0.08, 0.12, 0.14], row_padding=0,
    styled=[(f"{CHECKS}.Estado", "Color control", None)])

page.write()
page.set_canvas(CANVAS)
print(f"wrote {len(page.visuals)} visuals")
