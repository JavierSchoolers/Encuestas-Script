"""
link_marca_matriculas.py
─────────────────────────────────────────────────────────────────────────────
Rellena "Empresa - dashboard" en los boards de Encuestas usando la Marca
real del alumno, obtenida desde Matrículas FUNDAE y Particulares.

Cadena de datos:
  Matrículas (name = DNI)
    → conectar_tableros4 (relación → Sociedades)
      → conectar_tableros (relación → Cuentas)
        → name = Marca (ej: "Iberostar", "Grupotel"...)

Uso:
  python3 link_marca_matriculas.py --board egh       # Solo EGH
  python3 link_marca_matriculas.py --board cursos    # Solo Cursos
  python3 link_marca_matriculas.py                   # Ambos
  python3 link_marca_matriculas.py --dry-run         # Solo mostrar
  python3 link_marca_matriculas.py --force           # Sobrescribir existentes
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

# Boards fuente
MATRICULAS_BOARD   = 1407763206   # "Matrículas FUNDAE y Particulares"
MATRICULAS_CIF_COL = "conectar_tableros4"   # relación → Sociedades

SOCIEDADES_BOARD    = 1562861517  # "Sociedades"
SOCIEDADES_CTA_COL  = "conectar_tableros"   # relación → Cuentas

# Boards destino
ENCUESTAS_EGH    = 5093144633
EGH_DNI_COL      = "text_mm2fq73j"
EGH_EMPRESA_COL  = "text_mm2fka7y"   # "Cuenta empresa" (texto)

ENCUESTAS_CURSOS    = 5094417029
CURSOS_DNI_COL      = "text_mm2fhjgw"
CURSOS_EMPRESA_COL  = "text_mm2fc06a"   # "Empresa - dashboard" (texto)


# ══════════════════════════════════════════════════════════════════════════════
# MONDAY API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def monday_query(query, variables=None, retries=4):
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type":  "application/json",
        "API-Version":   "2024-10"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(retries):
        try:
            resp = requests.post(MONDAY_API, json=payload, headers=headers, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = (attempt + 1) * 15
                print(f"  ⚠️  HTTP {resp.status_code}, reintentando en {wait}s... ({attempt+1}/{retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                print(f"  ⚠️  GraphQL errors: {data['errors']}")
                return None
            return data.get("data")
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as e:
            if attempt < retries - 1:
                wait = (attempt + 1) * 10
                print(f"  ⚠️  Error conexión, reintentando en {wait}s...")
                time.sleep(wait)
            else:
                print(f"  ❌ Error tras {retries} intentos: {e}")
                raise
    return None


def fetch_all_items(board_id, col_ids):
    """Pagina todos los items de un board, devuelve lista de {id, name, column_values}."""
    all_items = []
    cursor = None
    page = 0

    while True:
        page += 1
        cols_json = json.dumps(col_ids)
        if cursor:
            query = """
            query($cursor: String!) {
                next_items_page(cursor: $cursor, limit: 500) {
                    cursor
                    items { id name column_values(ids: %s) { id text value } }
                }
            }""" % cols_json
            variables = {"cursor": cursor}
        else:
            query = """
            query($boardId: [ID!]!) {
                boards(ids: $boardId) {
                    items_page(limit: 500) {
                        cursor
                        items { id name column_values(ids: %s) { id text value } }
                    }
                }
            }""" % cols_json
            variables = {"boardId": [str(board_id)]}

        data = monday_query(query, variables)
        if not data:
            break

        page_data = (data.get("next_items_page") or
                     (data.get("boards") or [{}])[0].get("items_page") or {})
        items     = page_data.get("items", [])
        cursor    = page_data.get("cursor")

        all_items.extend(items)
        print(f"    Página {page}: {len(items)} items (total: {len(all_items)})")

        if not cursor or not items:
            break
        time.sleep(0.3)

    return all_items


def get_linked_ids(item, col_id):
    """Extrae los IDs vinculados de una columna board_relation."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            val = cv.get("value")
            if not val:
                return []
            try:
                parsed = json.loads(val)
                return [str(lp["linkedPulseId"])
                        for lp in parsed.get("linkedPulseIds", [])
                        if lp.get("linkedPulseId")]
            except Exception:
                return []
    return []


def get_item_names_by_ids(item_ids):
    """Consulta el nombre de items por sus IDs (en lotes de 100)."""
    id_to_name = {}
    batch_size = 100
    for i in range(0, len(item_ids), batch_size):
        batch = item_ids[i:i+batch_size]
        query = "query { items(ids: [%s]) { id name } }" % ", ".join(batch)
        data = monday_query(query)
        if data and data.get("items"):
            for item in data["items"]:
                id_to_name[str(item["id"])] = item["name"]
        time.sleep(0.3)
    return id_to_name


def normalize_dni(s):
    return (s or "").strip().upper().replace(" ", "").replace("-", "").replace(".", "")


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUIR MAPA DNI → MARCA
# ══════════════════════════════════════════════════════════════════════════════

def build_dni_to_marca():
    """
    Recorre Matrículas para construir {DNI_norm: marca}.
    Cadena: Matrículas(name=DNI) → Sociedad → Cuenta(name=Marca)
    """
    print("\n📥 Paso 1: Cargando Matrículas (DNI + relación CIF)...")
    matriculas = fetch_all_items(MATRICULAS_BOARD, [MATRICULAS_CIF_COL])
    print(f"   Total: {len(matriculas)} matrículas")

    # DNI → [sociedad_ids]
    dni_to_soc_ids = {}
    for item in matriculas:
        dni = normalize_dni(item.get("name", ""))
        if not dni:
            continue
        soc_ids = get_linked_ids(item, MATRICULAS_CIF_COL)
        if soc_ids:
            # Quedarse con la primera (una matrícula puede tener varias sociedades,
            # pero tomamos la más reciente = primera devuelta)
            dni_to_soc_ids.setdefault(dni, soc_ids[0])

    print(f"   DNIs con sociedad vinculada: {len(dni_to_soc_ids)}")

    # Obtener los IDs únicos de sociedades que necesitamos
    needed_soc_ids = list(set(dni_to_soc_ids.values()))
    print(f"\n📥 Paso 2: Cargando {len(needed_soc_ids)} Sociedades (relación Cuenta)...")

    # Cargar solo las sociedades necesarias en lotes
    soc_id_to_cta_ids = {}
    batch_size = 100
    for i in range(0, len(needed_soc_ids), batch_size):
        batch = needed_soc_ids[i:i+batch_size]
        query = """
        query {
            items(ids: [%s]) {
                id
                column_values(ids: ["%s"]) { id value }
            }
        }""" % (", ".join(batch), SOCIEDADES_CTA_COL)
        data = monday_query(query)
        if data and data.get("items"):
            for soc in data["items"]:
                cta_ids = get_linked_ids(soc, SOCIEDADES_CTA_COL)
                if cta_ids:
                    soc_id_to_cta_ids[str(soc["id"])] = cta_ids[0]
        time.sleep(0.3)
        if i % 500 == 0 and i > 0:
            print(f"    Procesadas {i} sociedades...")

    print(f"   Sociedades con cuenta vinculada: {len(soc_id_to_cta_ids)}")

    # Obtener nombres (= Marca) de todas las cuentas necesarias
    needed_cta_ids = list(set(soc_id_to_cta_ids.values()))
    print(f"\n📥 Paso 3: Obteniendo nombres de {len(needed_cta_ids)} Cuentas (= Marca)...")
    cta_id_to_marca = get_item_names_by_ids(needed_cta_ids)
    print(f"   Cuentas encontradas: {len(cta_id_to_marca)}")

    # Construir mapa final DNI → Marca
    dni_to_marca = {}
    for dni, soc_id in dni_to_soc_ids.items():
        cta_id = soc_id_to_cta_ids.get(soc_id)
        if not cta_id:
            continue
        marca = cta_id_to_marca.get(cta_id, "")
        if marca:
            dni_to_marca[dni] = marca

    print(f"\n✅ Mapa final: {len(dni_to_marca)} DNIs con Marca")
    return dni_to_marca


# ══════════════════════════════════════════════════════════════════════════════
# PROCESAR BOARD DE ENCUESTAS
# ══════════════════════════════════════════════════════════════════════════════

def process_encuestas_board(board_name, board_id, dni_col, empresa_col,
                             dni_to_marca, dry_run=False, force=False):
    print(f"\n{'─' * 70}")
    print(f"📋 {board_name} ({board_id})")

    items = fetch_all_items(board_id, [dni_col, empresa_col])
    print(f"   Total items: {len(items)}")

    written = 0
    skip_no_dni = 0
    skip_not_found = 0
    skip_already = 0
    errors = 0

    for item in items:
        dni = normalize_dni(
            next((cv["text"] for cv in item["column_values"] if cv["id"] == dni_col), "")
        )
        if not dni:
            skip_no_dni += 1
            continue

        marca = dni_to_marca.get(dni)
        if not marca:
            skip_not_found += 1
            continue

        existing = next((cv["text"] for cv in item["column_values"]
                         if cv["id"] == empresa_col), "") or ""
        if existing and not force:
            skip_already += 1
            continue

        written += 1
        if dry_run:
            if written <= 20:
                old = f" (antes: '{existing}')" if existing else ""
                print(f"   ✅ {item['name'][:70]} → {marca}{old}")
            elif written == 21:
                print("   ...")
        else:
            col_values = json.dumps({empresa_col: marca})
            query = """
            mutation($boardId: ID!, $itemId: ID!, $col: JSON!) {
                change_multiple_column_values(
                    board_id: $boardId, item_id: $itemId, column_values: $col
                ) { id }
            }"""
            result = monday_query(query, {
                "boardId": str(board_id),
                "itemId":  str(item["id"]),
                "col":     col_values
            })
            if result:
                if written <= 20 or written % 200 == 0:
                    print(f"   ✅ [{written}] {item['name'][:60]} → {marca}")
            else:
                errors += 1
                print(f"   ⚠️  Error: {item['name'][:60]}")
            time.sleep(0.35)

    print(f"\n   📊 RESUMEN {board_name} {'(DRY RUN)' if dry_run else ''}:")
    print(f"      Total items:       {len(items)}")
    print(f"      Sin DNI:           {skip_no_dni}")
    print(f"      DNI sin marca:     {skip_not_found}")
    print(f"      Ya con empresa:    {skip_already}")
    print(f"      Escritos:          {written}")
    if not dry_run:
        print(f"      Errores:           {errors}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Rellena Empresa-dashboard desde Matrículas → Sociedades → Marca"
    )
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar sin escribir")
    parser.add_argument("--board", choices=["egh", "cursos", "ambos"], default="ambos")
    parser.add_argument("--force", action="store_true",
                        help="Sobrescribir valores existentes")
    args = parser.parse_args()

    print(f"{'🔍 DRY RUN — ' if args.dry_run else ''}Rellenando Empresa desde Matrículas → Marca")
    if args.force:
        print("⚠️  FORCE: sobrescribirá valores existentes")
    print("=" * 70)

    # Construir mapa DNI → Marca (siempre, independiente del board destino)
    dni_to_marca = build_dni_to_marca()

    if args.board in ("egh", "ambos"):
        process_encuestas_board(
            "Encuestas: EGH", ENCUESTAS_EGH,
            EGH_DNI_COL, EGH_EMPRESA_COL,
            dni_to_marca, dry_run=args.dry_run, force=args.force
        )

    if args.board in ("cursos", "ambos"):
        process_encuestas_board(
            "Encuestas: Cursos", ENCUESTAS_CURSOS,
            CURSOS_DNI_COL, CURSOS_EMPRESA_COL,
            dni_to_marca, dry_run=args.dry_run, force=args.force
        )

    print(f"\n{'=' * 70}")
    print("✅ Proceso completado")


if __name__ == "__main__":
    main()
