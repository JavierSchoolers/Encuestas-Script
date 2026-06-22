"""
link_encuestas_alumnos.py
─────────────────────────────────────────────────────────────────────────────
Escribe la "Empresa - dashboard" y vincula "Alumno (rel)" en los boards de
Encuestas, buscando el alumno en el board Alumnos por DNI.

Para Encuestas: Cursos rellena también la columna board_relation "Alumno (rel)"
(board_relation_mm2fezr6) además del texto de empresa.

Uso:
  python3 link_encuestas_alumnos.py              # Ejecutar ambos boards
  python3 link_encuestas_alumnos.py --dry-run    # Solo mostrar matches
  python3 link_encuestas_alumnos.py --board egh  # Solo board EGH
  python3 link_encuestas_alumnos.py --board cursos  # Solo board Cursos
  python3 link_encuestas_alumnos.py --force      # Sobrescribir valores existentes
─────────────────────────────────────────────────────────────────────────────
"""

import os
import requests
import json
import sys
import time
import argparse

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

# Credencial desde variable de entorno / GitHub Secret (nunca en código).
MONDAY_TOKEN = os.environ.get("MONDAY_TOKEN", "")
MONDAY_API   = "https://api.monday.com/v2"

if not MONDAY_TOKEN:
    print("✗ Falta la variable de entorno MONDAY_TOKEN.", file=sys.stderr)
    sys.exit(2)

# Board IDs
ALUMNOS_BOARD    = 1388079440
CUENTAS_BOARD    = 1368310907
ENCUESTAS_CURSOS = 5094417029
ENCUESTAS_EGH    = 5093144633

# Column IDs — Alumnos
ALUMNOS_DNI_COL      = "texto24"                    # "Número documento"
ALUMNOS_EMPRESA_COL  = "text_mm0xmv5p"              # "Empresa actualizada" (texto directo)

# Column IDs — Encuestas: Cursos
CURSOS_DNI_COL       = "text_mm2fhjgw"
CURSOS_EMPRESA_COL   = "text_mm2fc06a"             # "Empresa - dashboard" (texto)
CURSOS_RELATION_COL  = "board_relation_mm2fezr6"   # "Alumno (rel)" → board Alumnos

# Column IDs — Encuestas: EGH
EGH_DNI_COL          = "text_mm2fq73j"
EGH_EMPRESA_COL      = "text_mm2fka7y"    # "Cuenta empresa" (texto)


# ══════════════════════════════════════════════════════════════════════════════
# MONDAY API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def monday_query(query, variables=None, retries=3):
    """Ejecuta una query GraphQL contra Monday.com con retry logic."""
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(retries):
        try:
            resp = requests.post(MONDAY_API, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                print(f"  ⚠️  GraphQL errors: {data['errors']}")
                return None
            return data.get("data")
        except requests.exceptions.HTTPError as e:
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = (attempt + 1) * 15
                print(f"  ⚠️  HTTP {resp.status_code}, reintentando en {wait}s... ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                print(f"  ❌ HTTP error: {e}")
                raise
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                wait = (attempt + 1) * 8
                print(f"  ⚠️  Error de conexión, reintentando en {wait}s... ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                print(f"  ❌ Error de conexión tras {retries} intentos: {e}")
                raise


def fetch_all_items(board_id, columns_ids):
    """Obtiene todos los items de un board con paginación por cursor."""
    all_items = []
    cursor = None
    page = 0

    while True:
        page += 1
        if cursor:
            query = """
            query ($cursor: String!) {
                next_items_page(cursor: $cursor, limit: 500) {
                    cursor
                    items {
                        id
                        name
                        column_values(ids: %s) {
                            id
                            text
                            value
                        }
                    }
                }
            }
            """ % json.dumps(columns_ids)
            variables = {"cursor": cursor}
        else:
            query = """
            query ($boardId: [ID!]!) {
                boards(ids: $boardId) {
                    items_page(limit: 500) {
                        cursor
                        items {
                            id
                            name
                            column_values(ids: %s) {
                                id
                                text
                                value
                            }
                        }
                    }
                }
            }
            """ % json.dumps(columns_ids)
            variables = {"boardId": [str(board_id)]}

        data = monday_query(query, variables)
        if not data:
            break

        if cursor:
            page_data = data.get("next_items_page", {})
        else:
            boards = data.get("boards", [])
            if not boards:
                break
            page_data = boards[0].get("items_page", {})

        items = page_data.get("items", [])
        new_cursor = page_data.get("cursor")

        all_items.extend(items)
        print(f"  Página {page}: {len(items)} items (total: {len(all_items)})")

        if not new_cursor or not items:
            break
        cursor = new_cursor
        time.sleep(0.3)

    return all_items


def get_column_value(item, col_id):
    """Extrae el valor de texto de una columna de un item."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            return (cv.get("text") or "").strip()
    return ""


def get_column_json(item, col_id):
    """Extrae el valor JSON de una columna."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            val = cv.get("value")
            if val:
                try:
                    return json.loads(val)
                except:
                    pass
    return None


def set_columns(board_id, item_id, col_values_dict):
    """Establece múltiples columnas en un item."""
    col_values = json.dumps(col_values_dict)
    query = """
    mutation ($boardId: ID!, $itemId: ID!, $colValues: JSON!) {
        change_multiple_column_values(
            board_id: $boardId,
            item_id: $itemId,
            column_values: $colValues
        ) { id }
    }
    """
    variables = {
        "boardId": str(board_id),
        "itemId": str(item_id),
        "colValues": col_values
    }
    return monday_query(query, variables)


def set_text_column(board_id, item_id, col_id, text_value):
    """Establece un valor de texto en una columna (wrapper de set_columns)."""
    return set_columns(board_id, item_id, {col_id: text_value})


def is_relation_set(item, col_id):
    """Devuelve True si la columna board_relation ya tiene un item vinculado."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            val = cv.get("value")
            if not val:
                return False
            try:
                parsed = json.loads(val)
                linked = parsed.get("linkedPulseIds", [])
                return len(linked) > 0
            except Exception:
                return False
    return False


def normalize_dni(dni_str):
    """Normaliza un DNI para comparación."""
    if not dni_str:
        return ""
    return dni_str.strip().upper().replace(" ", "").replace("-", "").replace(".", "")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Vincula Alumno (rel) y Empresa en Encuestas por DNI")
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar matches sin escribir")
    parser.add_argument("--board", choices=["cursos", "egh", "ambos"], default="ambos",
                        help="Board(s) a procesar (default: ambos)")
    parser.add_argument("--force", action="store_true",
                        help="Sobrescribir aunque ya tenga valor")
    args = parser.parse_args()

    print(f"{'🔍 DRY RUN — ' if args.dry_run else ''}Vinculando Alumno (rel) + Empresa en Encuestas")
    if args.force:
        print("⚠️  FORCE: se sobrescribirán valores existentes")
    print("=" * 70)

    # ── 1. Cargar items del board Alumnos (DNI + Empresa) ──
    print(f"\n📥 Cargando Alumnos ({ALUMNOS_BOARD}) con DNI y Empresa...")
    alumnos_items = fetch_all_items(ALUMNOS_BOARD, [ALUMNOS_DNI_COL, ALUMNOS_EMPRESA_COL])
    print(f"   Total: {len(alumnos_items)} alumnos")

    # ── 2. Construir mapas DNI → empresa  y  DNI → item_id del alumno ──
    dni_to_empresa   = {}
    dni_to_alumno_id = {}
    empty_dnis = 0
    no_empresa = 0

    for item in alumnos_items:
        dni = normalize_dni(get_column_value(item, ALUMNOS_DNI_COL))
        if not dni:
            empty_dnis += 1
            continue
        dni_to_alumno_id[dni] = item["id"]
        empresa = get_column_value(item, ALUMNOS_EMPRESA_COL)
        if empresa:
            dni_to_empresa[dni] = empresa
        else:
            no_empresa += 1

    print(f"\n📊 Mapas DNI:")
    print(f"   DNIs únicos:         {len(dni_to_alumno_id)}")
    print(f"   DNIs con empresa:    {len(dni_to_empresa)}")
    print(f"   Alumnos sin DNI:     {empty_dnis}")
    print(f"   Alumnos sin empresa: {no_empresa}")

    # ── 3. Procesar boards de Encuestas ──
    if args.board in ("cursos", "ambos"):
        _process_cursos(args, dni_to_alumno_id, dni_to_empresa)
    if args.board in ("egh", "ambos"):
        _process_egh(args, dni_to_empresa)


def _process_cursos(args, dni_to_alumno_id, dni_to_empresa):
    """Cursos: rellena Alumno (rel) + Empresa - dashboard."""
    print(f"\n{'─' * 70}")
    print(f"📋 Encuestas: Cursos ({ENCUESTAS_CURSOS})")
    print(f"   Columnas: Alumno (rel) [{CURSOS_RELATION_COL}] + Empresa [{CURSOS_EMPRESA_COL}]")

    cols = [CURSOS_DNI_COL, CURSOS_EMPRESA_COL, CURSOS_RELATION_COL]
    enc_items = fetch_all_items(ENCUESTAS_CURSOS, cols)
    print(f"   Total items: {len(enc_items)}")

    # Solo items de la sección "Por Encuesta" tienen alumno/DNI (los otros no tienen DNI)
    matched_rel = 0
    matched_emp = 0
    not_found   = 0
    no_dni      = 0
    skip_rel    = 0
    skip_emp    = 0
    errors      = 0

    for item in enc_items:
        enc_dni = normalize_dni(get_column_value(item, CURSOS_DNI_COL))
        if not enc_dni:
            no_dni += 1
            continue

        alumno_id = dni_to_alumno_id.get(enc_dni)
        empresa   = dni_to_empresa.get(enc_dni, "")

        if not alumno_id:
            not_found += 1
            continue

        # ¿Qué hay que actualizar?
        rel_set     = is_relation_set(item, CURSOS_RELATION_COL)
        empresa_set = bool(get_column_value(item, CURSOS_EMPRESA_COL))

        need_rel = not rel_set or args.force
        need_emp = (not empresa_set or args.force) and bool(empresa)

        if not need_rel:
            skip_rel += 1
        if not need_emp:
            skip_emp += 1

        if not need_rel and not need_emp:
            continue

        # Construir el dict de columnas a actualizar
        col_values = {}
        if need_rel:
            col_values[CURSOS_RELATION_COL] = {"item_ids": [int(alumno_id)]}
            matched_rel += 1
        if need_emp:
            col_values[CURSOS_EMPRESA_COL] = empresa
            matched_emp += 1

        if args.dry_run:
            parts = []
            if need_rel:  parts.append(f"rel→alumno {alumno_id}")
            if need_emp:  parts.append(f"empresa→{empresa}")
            total = matched_rel + matched_emp
            if total <= 20:
                print(f"   ✅ {item['name'][:70]} | {' | '.join(parts)}")
            elif total == 21:
                print(f"   ...")
        else:
            result = set_columns(ENCUESTAS_CURSOS, item["id"], col_values)
            if result:
                total = matched_rel + matched_emp
                if total <= 20 or total % 200 == 0:
                    parts = []
                    if need_rel: parts.append(f"🔗rel")
                    if need_emp: parts.append(f"🏢{empresa}")
                    print(f"   ✅ [{total}] {item['name'][:55]} | {' | '.join(parts)}")
            else:
                errors += 1
                print(f"   ⚠️  Error: {item['name'][:60]}")
            time.sleep(0.35)

    print(f"\n   📊 RESUMEN Cursos {'(DRY RUN)' if args.dry_run else ''}:")
    print(f"      Items totales:          {len(enc_items)}")
    print(f"      Sin DNI (prog/módulo):  {no_dni}")
    print(f"      DNI no encontrado:      {not_found}")
    print(f"      Ya con relación:        {skip_rel}")
    print(f"      Ya con empresa:         {skip_emp}")
    print(f"      Relaciones escritas:    {matched_rel}")
    print(f"      Empresas escritas:      {matched_emp}")
    if not args.dry_run:
        print(f"      Errores:                {errors}")


def _process_egh(args, dni_to_empresa):
    """EGH: solo rellena empresa (texto), no tiene columna de relación configurable aquí)."""
    print(f"\n{'─' * 70}")
    print(f"📋 Encuestas: EGH ({ENCUESTAS_EGH})")

    enc_items = fetch_all_items(ENCUESTAS_EGH, [EGH_DNI_COL, EGH_EMPRESA_COL])
    print(f"   Total items: {len(enc_items)}")

    matched = 0
    not_found = 0
    no_dni = 0
    already_set = 0
    errors = 0

    for item in enc_items:
        enc_dni = normalize_dni(get_column_value(item, EGH_DNI_COL))
        if not enc_dni:
            no_dni += 1
            continue

        existing = get_column_value(item, EGH_EMPRESA_COL)
        if existing and not args.force:
            already_set += 1
            continue

        empresa = dni_to_empresa.get(enc_dni)
        if not empresa:
            not_found += 1
            continue

        matched += 1
        if args.dry_run:
            if matched <= 15:
                print(f"   ✅ {item['name'][:80]} → {empresa}")
        else:
            result = set_text_column(ENCUESTAS_EGH, item["id"], EGH_EMPRESA_COL, empresa)
            if result:
                if matched <= 15 or matched % 100 == 0:
                    print(f"   ✅ [{matched}] → {empresa}")
            else:
                errors += 1
                print(f"   ⚠️  Error: {item['name'][:60]}")
            time.sleep(0.4)

    print(f"\n   📊 RESUMEN EGH {'(DRY RUN)' if args.dry_run else ''}:")
    print(f"      Items:              {len(enc_items)}")
    print(f"      Sin DNI:            {no_dni}")
    print(f"      Ya con empresa:     {already_set}")
    print(f"      Empresa encontrada: {matched}")
    print(f"      DNI sin empresa:    {not_found}")
    if not args.dry_run:
        print(f"      Errores:            {errors}")


if __name__ == "__main__":
    main()
