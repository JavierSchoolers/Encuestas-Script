"""
sync_mirror_empresa.py
─────────────────────────────────────────────────────────────────────────────
Sincroniza "Cuenta empresa" en los boards de Encuestas leyendo la relación
con el board Alumnos.

Flujo:
  1. Lee los items de Encuestas con su columna board_relation (Alumno vinculado)
  2. Recoge todos los IDs de alumnos vinculados
  3. Consulta el board Alumnos para obtener "Empresa actualizada" de cada uno
  4. Escribe la empresa en la columna de texto "Cuenta empresa" de Encuestas

Así la columna espejo y la de texto siempre coinciden, y el dashboard
puede leer la de texto (que la API sí soporta).

Uso:
  python3 sync_mirror_empresa.py              # Sincronizar ambos boards
  python3 sync_mirror_empresa.py --dry-run    # Solo mostrar qué se copiaría
  python3 sync_mirror_empresa.py --board egh  # Solo board EGH
  python3 sync_mirror_empresa.py --board cursos  # Solo board Cursos
  python3 sync_mirror_empresa.py --force      # Sobrescribir aunque ya tenga valor
─────────────────────────────────────────────────────────────────────────────
"""

import os
import sys
import requests
import json
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

# ── Board Alumnos ──
ALUMNOS_BOARD       = 1388079440
ALUMNOS_EMPRESA_COL = "text_mm0xmv5p"       # "Empresa actualizada" (texto directo)

# ── Board: Encuestas EGH (5093144633) ──
EGH_BOARD          = 5093144633
EGH_RELATION_COL   = "board_relation_mm2fhexa"   # Relación con Alumnos
EGH_TEXT_COL       = "text_mm2fka7y"              # Columna texto "Cuenta empresa"

# ── Board: Encuestas Cursos (5094417029) ──
CURSOS_BOARD         = 5094417029
CURSOS_RELATION_COLS = ["board_relation_mm2fezr6", "board_relation_mm2fs9v"]  # Dos relaciones
CURSOS_TEXT_COL      = "text_mm2fc06a"             # Columna texto "Cuenta empresa"


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
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as e:
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


def get_column_text(item, col_id):
    """Extrae el valor de texto de una columna."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            return (cv.get("text") or "").strip()
    return ""


def get_linked_alumno_ids(item, relation_col_ids):
    """Extrae los IDs de alumnos vinculados desde una o más columnas de relación."""
    if isinstance(relation_col_ids, str):
        relation_col_ids = [relation_col_ids]
    alumno_ids = []
    for cv in item.get("column_values", []):
        if cv["id"] in relation_col_ids:
            val = cv.get("value")
            if val:
                try:
                    parsed = json.loads(val)
                    # board_relation value format: {"linkedPulseIds": [{"linkedPulseId": 12345}]}
                    linked = parsed.get("linkedPulseIds", [])
                    for lp in linked:
                        aid = lp.get("linkedPulseId")
                        if aid:
                            alumno_ids.append(str(aid))
                except (json.JSONDecodeError, TypeError):
                    pass
    return alumno_ids


def fetch_alumnos_empresa(alumno_ids):
    """Consulta el board Alumnos para obtener 'Empresa actualizada' de una lista de IDs.
    Devuelve un dict {alumno_id: empresa}."""
    if not alumno_ids:
        return {}

    result = {}
    # Procesar en lotes de 100 IDs
    batch_size = 100
    for i in range(0, len(alumno_ids), batch_size):
        batch = alumno_ids[i:i+batch_size]
        ids_str = ", ".join(batch)
        query = """
        query {
            items(ids: [%s]) {
                id
                column_values(ids: ["%s"]) {
                    id
                    text
                }
            }
        }
        """ % (ids_str, ALUMNOS_EMPRESA_COL)

        data = monday_query(query)
        if data and data.get("items"):
            for item in data["items"]:
                empresa = ""
                for cv in item.get("column_values", []):
                    if cv["id"] == ALUMNOS_EMPRESA_COL:
                        empresa = (cv.get("text") or "").strip()
                if empresa:
                    result[str(item["id"])] = empresa

        if i + batch_size < len(alumno_ids):
            time.sleep(0.3)

    return result


def set_text_column(board_id, item_id, col_id, text_value):
    """Establece un valor de texto en una columna."""
    col_values = json.dumps({col_id: text_value})
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


# ══════════════════════════════════════════════════════════════════════════════
# PROCESAR BOARD
# ══════════════════════════════════════════════════════════════════════════════

def process_board(board_name, board_id, relation_col_ids, text_col_id, dry_run=False, force=False):
    """Procesa un board: sigue relación → Alumno → Empresa → escribe texto."""
    print(f"\n{'─' * 70}")
    print(f"📋 {board_name} ({board_id})")

    # Columnas a pedir: relación(es) + texto destino
    if isinstance(relation_col_ids, str):
        cols_to_fetch = [relation_col_ids, text_col_id]
    else:
        cols_to_fetch = list(relation_col_ids) + [text_col_id]

    # 1. Obtener todos los items de Encuestas
    print(f"\n  📥 Cargando items de {board_name}...")
    items = fetch_all_items(board_id, cols_to_fetch)
    print(f"   Total items: {len(items)}")

    # 2. Recoger todos los IDs de alumnos vinculados
    print(f"\n  🔗 Extrayendo relaciones con Alumnos...")
    all_alumno_ids = set()
    item_alumno_map = {}  # encuesta_item_id -> [alumno_ids]

    for item in items:
        linked = get_linked_alumno_ids(item, relation_col_ids)
        if linked:
            item_alumno_map[item["id"]] = linked
            all_alumno_ids.update(linked)

    items_with_relation = len(item_alumno_map)
    items_without_relation = len(items) - items_with_relation
    print(f"   Items con relación: {items_with_relation}")
    print(f"   Items sin relación: {items_without_relation}")
    print(f"   Alumnos únicos vinculados: {len(all_alumno_ids)}")

    if not all_alumno_ids:
        print(f"\n   ⚠️  No hay relaciones — nada que sincronizar")
        return 0, 0

    # 3. Consultar empresas de los alumnos vinculados
    print(f"\n  🏢 Consultando empresas de {len(all_alumno_ids)} alumnos...")
    alumno_empresa = fetch_alumnos_empresa(list(all_alumno_ids))
    print(f"   Alumnos con empresa: {len(alumno_empresa)}")
    print(f"   Alumnos sin empresa: {len(all_alumno_ids) - len(alumno_empresa)}")

    # 4. Escribir empresa en columna texto
    print(f"\n  📝 Actualizando columna texto...")
    copied = 0
    skipped_no_relation = 0
    skipped_no_empresa = 0
    skipped_same_value = 0
    skipped_already_set = 0
    errors = 0

    for item in items:
        alumno_ids = item_alumno_map.get(item["id"], [])
        if not alumno_ids:
            skipped_no_relation += 1
            continue

        # Buscar empresa del alumno vinculado (usar el primero que tenga)
        empresa = ""
        for aid in alumno_ids:
            empresa = alumno_empresa.get(aid, "")
            if empresa:
                break

        if not empresa:
            skipped_no_empresa += 1
            continue

        text_val = get_column_text(item, text_col_id)

        # Ya tiene el mismo valor
        if text_val == empresa:
            skipped_same_value += 1
            continue

        # Ya tiene un valor distinto y no --force
        if text_val and not force:
            skipped_already_set += 1
            continue

        copied += 1

        if dry_run:
            change_type = "🔄" if text_val else "✅"
            if copied <= 25:
                old_info = f" (antes: '{text_val}')" if text_val else ""
                print(f"   {change_type} {item['name'][:65]} → {empresa}{old_info}")
            elif copied == 26:
                print(f"   ... y más")
        else:
            result = set_text_column(board_id, item["id"], text_col_id, empresa)
            if result:
                if copied <= 20 or copied % 50 == 0:
                    print(f"   ✅ [{copied}] {item['name'][:50]} → {empresa}")
            else:
                errors += 1
                print(f"   ⚠️  Error: {item['name'][:60]}")
            time.sleep(0.4)

    print(f"\n   📊 RESUMEN {board_name} {'(DRY RUN)' if dry_run else ''}:")
    print(f"      Total items:          {len(items)}")
    print(f"      Sin relación:         {skipped_no_relation}")
    print(f"      Relación sin empresa: {skipped_no_empresa}")
    print(f"      Ya con mismo valor:   {skipped_same_value}")
    if not force:
        print(f"      Ya con otro valor:    {skipped_already_set}")
    print(f"      Copiados/Actualiz.:   {copied}")
    if not dry_run:
        print(f"      Errores:              {errors}")

    return copied, errors


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Sincroniza 'Cuenta empresa' desde relación Alumnos → texto en Encuestas"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo mostrar qué se copiaría, sin escribir")
    parser.add_argument("--board", choices=["cursos", "egh", "ambos"], default="ambos",
                        help="Board(s) a procesar (default: ambos)")
    parser.add_argument("--force", action="store_true",
                        help="Sobrescribir columna texto aunque ya tenga un valor distinto")
    args = parser.parse_args()

    mode = "🔍 DRY RUN" if args.dry_run else "📝 SYNC"
    print(f"{mode} — Sincronizando 'Cuenta empresa' (relación → texto)")
    if args.force:
        print("⚠️  FORCE: se sobrescribirán valores existentes distintos")
    print("=" * 70)

    total_copied = 0
    total_errors = 0

    if args.board in ("egh", "ambos"):
        c, e = process_board(
            "Encuestas: EGH", EGH_BOARD,
            EGH_RELATION_COL, EGH_TEXT_COL,
            dry_run=args.dry_run, force=args.force
        )
        total_copied += c
        total_errors += e

    if args.board in ("cursos", "ambos"):
        c, e = process_board(
            "Encuestas: Cursos", CURSOS_BOARD,
            CURSOS_RELATION_COLS, CURSOS_TEXT_COL,
            dry_run=args.dry_run, force=args.force
        )
        total_copied += c
        total_errors += e

    print(f"\n{'=' * 70}")
    print(f"✅ TOTAL: {total_copied} valores copiados, {total_errors} errores")


if __name__ == "__main__":
    main()
