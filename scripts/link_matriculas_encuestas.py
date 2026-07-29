"""
link_matriculas_encuestas.py
─────────────────────────────────────────────────────────────────────────────
Vincula cada ítem "Por Encuesta" de los boards de Encuestas con su MATRÍCULA
exacta, rellenando la columna board_relation "Matrículas FUNDAE y Particulares".

Al poner esa relación, Monday rellena SOLO los mirrors de la encuesta:
  · "Cuenta"        (reflejo77 de la matrícula)
  · "ID_Evolcampus" (texto_1__1 de la matrícula)
Es decir: asociando la matrícula, la cuenta y el ID_Evolcampus vienen dados.

CLAVE DE CRUCE = DNI + Grupo EvolCampus (group_id)
  - La matrícula guarda su group_id en el mirror "Grupo Evolcampus" (reflejo_1__1)
    y el DNI en "Nº documento" (lookup4).
  - La encuesta guarda el DNI (text) y el NOMBRE del grupo (text). El nombre se
    traduce a group_id con getCoursesGroups de EvolCampus (misma fuente de la que
    el sync sacó ese nombre), así el cruce es 1:1 por ID (inmune al nombre del
    programa y a que un alumno tenga varias matrículas).

Boards (alcance actual: Cursos + EGH; Subvenciones queda para después):
  · Cursos 5094417029 → relación board_relation_mm5p6dmp → Matrículas [1407763206, 5030543348]
  · EGH    5093144633 → relación board_relation_mm5pgak4 → Matrículas [1407763206]

Uso:
  python3 link_matriculas_encuestas.py                 # cursos + egh
  python3 link_matriculas_encuestas.py --board cursos
  python3 link_matriculas_encuestas.py --board egh
  python3 link_matriculas_encuestas.py --dry-run       # no escribe, solo informa
  python3 link_matriculas_encuestas.py --force         # re-escribe aunque ya haya relación

Env: MONDAY_TOKEN, EVOLCAMPUS_CLIENT_ID, EVOLCAMPUS_KEY.
     LINK_MAX_SECONDS (opcional, tope de tiempo de escritura; def. 10800 = 3h).
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import time
import json
import argparse
import requests

# Reutilizamos el cliente Monday robusto (retries/rate-limit) y los helpers de
# escritura/lectura ya probados del linker de alumnos.
from link_encuestas_alumnos import (
    monday_query, fetch_all_items, set_columns_batch,
    is_relation_set, get_column_value, normalize_dni,
)

EVOLCAMPUS_CLIENT_ID = os.environ.get("EVOLCAMPUS_CLIENT_ID", "")
EVOLCAMPUS_KEY       = os.environ.get("EVOLCAMPUS_KEY", "")
EVOLCAMPUS_API       = "https://api.evolcampus.com/api/v1"

# ── Boards / columnas ────────────────────────────────────────────────────────
MAT_FUNDAE = "1407763206"
MAT_SUBV   = "5030543348"

# Columnas de MATRÍCULAS (mismas en ambos boards):
MAT_DNI_COL   = "lookup4"        # "Nº documento" (mirror)
MAT_GRUPO_COL = "reflejo_1__1"   # "Grupo Evolcampus" (mirror → group_id numérico)

# Config por board de Encuestas: DNI, Grupo (nombre), relación a matrícula, y a qué
# boards de matrículas puede apuntar esa relación.
ENC_BOARDS = {
    "cursos": {
        "board_id":  "5094417029",
        "label":     "Cursos",
        "dni_col":   "text_mm2fhjgw",
        "grupo_col": "text_mm29712e",
        "rel_col":   "board_relation_mm5p6dmp",
        "mat_boards": [MAT_FUNDAE, MAT_SUBV],
    },
    "egh": {
        "board_id":  "5093144633",
        "label":     "EGH",
        "dni_col":   "text_mm2fq73j",
        "grupo_col": "text_mm1d5xhk",
        "rel_col":   "board_relation_mm5pgak4",
        "mat_boards": [MAT_FUNDAE],
    },
}


# ── EvolCampus: mapa nombre de grupo → group_id ───────────────────────────────
def _evol_token():
    if not EVOLCAMPUS_CLIENT_ID or not EVOLCAMPUS_KEY:
        print("✗ Falta EVOLCAMPUS_CLIENT_ID / EVOLCAMPUS_KEY.", file=sys.stderr)
        sys.exit(2)
    r = requests.post(f"{EVOLCAMPUS_API}/token",
                      json={"clientid": EVOLCAMPUS_CLIENT_ID, "key": EVOLCAMPUS_KEY},
                      timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def _norm_group(s):
    """Normaliza un nombre de grupo para comparar (minúsculas, espacios colapsados)."""
    return re.sub(r'\s+', ' ', str(s or '').strip().lower())


def build_group_name_to_id(retries=4):
    """Mapa {nombre_grupo_normalizado: group_id} desde getCoursesGroups. Los nombres
    de grupo que se repiten en más de un group_id se marcan AMBIGUOS (None) y no se
    usan para cruzar (no inventamos)."""
    token = _evol_token()
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    for attempt in range(retries):
        resp = requests.get(f"{EVOLCAMPUS_API}/getCoursesGroups", headers=headers, timeout=60)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep((attempt + 1) * 10); continue
        resp.raise_for_status()
        data = resp.json(); break
    items = data if isinstance(data, list) else data.get("courses", data.get("groups", data.get("data", [])))
    name_to_id = {}
    ambiguous = set()
    n_groups = 0
    for item in items or []:
        for group in item.get("groups", []):
            gid = group.get("groupid")
            gname = _norm_group(group.get("group", ""))
            if gid is None or not gname:
                continue
            n_groups += 1
            gid = str(gid)
            if gname in name_to_id and name_to_id[gname] != gid:
                ambiguous.add(gname)
            else:
                name_to_id[gname] = gid
    for gname in ambiguous:
        name_to_id.pop(gname, None)
    print(f"   EvolCampus: {n_groups} grupos · nombres únicos usables: {len(name_to_id)} · ambiguos descartados: {len(ambiguous)}")
    return name_to_id


# ── Matrículas: mapa (DNI, group_id) → item_id de la matrícula ────────────────
def build_matricula_index(mat_board_ids):
    """Lee las matrículas (mirrors DNI + Grupo Evolcampus por GraphQL crudo) y devuelve
    {(normDNI, group_id): matricula_item_id}. Una matrícula puede listar varios group_id
    (se indexa cada uno). Colisiones (mismo DNI+grupo en 2 matrículas) → se queda la 1ª."""
    idx = {}
    collisions = 0
    cols = json.dumps([MAT_DNI_COL, MAT_GRUPO_COL])
    frag = ('column_values(ids: ' + cols + ') { id ... on MirrorValue { display_value } }')

    def _process(board_id, items):
        nonlocal collisions
        for it in items:
            vals = {cv["id"]: (cv.get("display_value") or "") for cv in it.get("column_values", [])}
            dni = normalize_dni(vals.get(MAT_DNI_COL, ""))
            if not dni:
                continue
            for part in re.split(r'[,;\s]+', str(vals.get(MAT_GRUPO_COL, "") or "")):
                if not part.isdigit():
                    continue
                key = (dni, part)
                if key in idx:
                    collisions += 1
                    continue
                idx[key] = it["id"]

    for board_id in mat_board_ids:
        q_first = ('query ($b: ID!) { boards(ids: [$b]) { items_page(limit: 500) { cursor '
                   'items { id ' + frag + ' } } } }')
        data = monday_query(q_first, {"b": board_id})
        n = 0
        if data and data.get("boards"):
            page = data["boards"][0]["items_page"]
            _process(board_id, page.get("items", [])); n += len(page.get("items", []))
            cursor = page.get("cursor")
            q_next = ('query ($c: String!) { next_items_page(limit: 500, cursor: $c) { cursor '
                      'items { id ' + frag + ' } } }')
            while cursor:
                dn = monday_query(q_next, {"c": cursor})
                if not dn or not dn.get("next_items_page"):
                    break
                np = dn["next_items_page"]
                _process(board_id, np.get("items", [])); n += len(np.get("items", []))
                cursor = np.get("cursor")
        print(f"   Matrículas board {board_id}: {n} items leídos")
    print(f"   Índice (DNI+grupo) → matrícula: {len(idx)} claves · colisiones DNI+grupo ignoradas: {collisions}")
    return idx


# ── Proceso por board de Encuestas ────────────────────────────────────────────
def process_board(cfg, name_to_gid, args):
    board_id = cfg["board_id"]
    label    = cfg["label"]
    dni_col  = cfg["dni_col"]
    grp_col  = cfg["grupo_col"]
    rel_col  = cfg["rel_col"]

    print(f"\n{'─' * 70}")
    print(f"📋 Encuestas: {label} ({board_id}) · relación [{rel_col}] → matrículas {cfg['mat_boards']}")

    mat_idx = build_matricula_index(cfg["mat_boards"])

    # NOTA: no usamos el filtro server-side is_empty sobre board_relation: en API
    # 2024-10 no filtra (devuelve el board entero). Leemos todo y filtramos en cliente.
    enc_items = fetch_all_items(board_id, [dni_col, grp_col, rel_col])
    print(f"   Encuestas leídas: {len(enc_items)}")

    updates = []
    no_dni = no_grupo = grp_no_gid = not_found = already = 0
    for item in enc_items:
        dni = normalize_dni(get_column_value(item, dni_col))
        if not dni:
            no_dni += 1            # filas Por Módulo / Por Grupo (sin alumno)
            continue
        if is_relation_set(item, rel_col) and not args.force:
            already += 1
            continue
        gname = _norm_group(get_column_value(item, grp_col))
        if not gname:
            no_grupo += 1
            continue
        gid = name_to_gid.get(gname)
        if not gid:
            grp_no_gid += 1        # nombre de grupo no está (o es ambiguo) en EvolCampus
            continue
        mid = mat_idx.get((dni, gid))
        if not mid:
            not_found += 1         # no hay matrícula con ese DNI+grupo
            continue
        updates.append((item["id"], {rel_col: {"item_ids": [int(mid)]}}))

    print(f"\n   📊 {label} {'(DRY RUN)' if args.dry_run else ''}:")
    print(f"      Sin DNI (prog/módulo):     {no_dni}")
    print(f"      Ya con relación:           {already}")
    print(f"      Sin grupo:                 {no_grupo}")
    print(f"      Grupo sin group_id:        {grp_no_gid}")
    print(f"      DNI+grupo sin matrícula:   {not_found}")
    print(f"      A vincular:                {len(updates)}")

    if args.dry_run:
        for iid, cv in updates[:20]:
            print(f"      [dry-run] enc {iid} → matrícula {cv[rel_col]['item_ids'][0]}")
        return
    if not updates:
        print("      ✓ Nada que escribir.")
        return
    budget = int(os.environ.get("LINK_MAX_SECONDS", "10800"))
    written = set_columns_batch(board_id, updates, max_seconds=budget)
    print(f"      ✅ Vinculadas {written}/{len(updates)} encuestas (relación a matrícula)")


def main():
    parser = argparse.ArgumentParser(description="Vincula Encuestas → Matrícula por DNI + Grupo EvolCampus")
    parser.add_argument("--board", choices=["cursos", "egh", "ambos"], default="ambos",
                        help="Board(s) a procesar (default: ambos = cursos + egh)")
    parser.add_argument("--dry-run", action="store_true", help="No escribe; solo informa de los cruces")
    parser.add_argument("--force", action="store_true", help="Re-escribe aunque ya tenga relación")
    args = parser.parse_args()

    print(f"{'🔍 DRY RUN — ' if args.dry_run else ''}Vinculando Encuestas → Matrícula (DNI + Grupo EvolCampus)")
    if args.force:
        print("⚠️  FORCE: se sobrescribirán relaciones existentes")
    print("=" * 70)

    print("\n[0] Mapa nombre de grupo → group_id (EvolCampus getCoursesGroups)…")
    name_to_gid = build_group_name_to_id()
    if not name_to_gid:
        print("✗ No se pudo construir el mapa de grupos de EvolCampus.", file=sys.stderr)
        sys.exit(2)

    boards = ["cursos", "egh"] if args.board == "ambos" else [args.board]
    for b in boards:
        process_board(ENC_BOARDS[b], name_to_gid, args)

    print("\n✅ Fin.")


if __name__ == "__main__":
    main()
