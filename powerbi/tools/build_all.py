"""Regenerates every report page. Run from this folder: python build_all.py"""
import runpy

for page in ("page_temporada", "page_partidos", "page_jugadores", "page_en_vivo", "page_salud"):
    print(page, end=": ")
    runpy.run_path(f"{page}.py", run_name="__main__")
