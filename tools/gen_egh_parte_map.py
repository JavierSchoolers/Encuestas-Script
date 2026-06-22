#!/usr/bin/env python3
"""
gen_egh_parte_map.py
─────────────────────────────────────────────────────────────────────────────
Genera el objeto JS `EGH_PARTE_MAP` (tema → nº de parte 1/2/3) a partir del Excel
de estructura del Máster EGH, para pegarlo en la función de Netlify
`netlify/functions/monday-encuestas-build-background.js`.

Replica la lógica de la compañera (monday_to_dashboard.py · _build_egh_parte_map):
hoja "Estructura"; la columna A marca "Parte 1./2./3." (se arrastra hacia abajo)
y la columna C es el "tema". Mapea {tema.lower(): parte}.

Uso:
  python3 gen_egh_parte_map.py "Control producción máster EGH_actualizado.xlsx"

Requiere: pip install openpyxl
"""
import sys, json

def build(xlsx_path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Estructura"]
    parte_map, cur = {}, None
    for row in ws.iter_rows(min_row=2, values_only=True):
        a = str(row[0] or "").strip()
        if a.startswith("Parte 1."):   cur = 1
        elif a.startswith("Parte 2."): cur = 2
        elif a.startswith("Parte 3."): cur = 3
        tema = str(row[2] or "").strip()
        if tema and cur and tema.lower() != "temas":
            parte_map[tema.lower()] = cur
    # Alias manual de la compañera (Excel "a huésped" vs Monday "al huésped").
    parte_map["3.6. tecnología aplicada al huésped"] = 1
    return parte_map

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 gen_egh_parte_map.py <ruta_al_xlsx>", file=sys.stderr)
        sys.exit(1)
    pm = build(sys.argv[1])
    # JSON es objeto JS válido; claves ordenadas para diffs estables.
    print("const EGH_PARTE_MAP = " +
          json.dumps(pm, ensure_ascii=False, indent=2, sort_keys=True) + ";")
    print(f"// {len(pm)} temas", file=sys.stderr)
