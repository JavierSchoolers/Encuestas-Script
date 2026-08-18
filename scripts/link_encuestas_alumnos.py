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
  python3 link_encuestas_alumnos.py --board subvenciones  # Solo board Subvenciones (5100940645)
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
# "Encuestas: Subvenciones RSK y AEHCOS" — clon estructural del board Cursos
# (mismos IDs de grupo y de columna) → se procesa con las mismas constantes CURSOS_*.
ENCUESTAS_SUBV   = 5100940645

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

def _rate_wait_from_errors(data):
    """Si el 'errors' del body es de rate-limit / complejidad, devuelve los segundos a
    esperar (usa retry_in_seconds de Monday si viene); si no lo es, None."""
    try:
        for e in (data.get("errors") or []):
            blob = (str(e.get("message", "")) + " " + str(e.get("extensions", {}))).lower()
            if any(k in blob for k in ("complexity", "rate limit", "minute", "budget", "too many")):
                ri = (e.get("extensions", {}) or {}).get("retry_in_seconds")
                return int(ri) if ri else 20
        top = (str(data.get("error_code", "")) + " " + str(data.get("error_message", ""))).lower()
        if any(k in top for k in ("complexity", "rate", "minute", "budget")):
            return int(data.get("retry_in_seconds") or 20)
    except Exception:
        pass
    return None


def monday_query(query, variables=None, retries=6):
    """Ejecuta una query/mutación GraphQL contra Monday con retry logic.
    v60fp · Honra el rate-limit real de Monday (cabecera Retry-After en 429 y
    retry_in_seconds en errores de complejidad) en vez de esperar fijo 15/30/45 s, y
    sube los reintentos (6) para que los lotes de escritura no caigan a 1×1."""
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            resp = requests.post(MONDAY_API, json=payload, headers=headers, timeout=60)

            # 429 → esperar lo que diga Monday (Retry-After), no un fijo largo.
            if resp.status_code == 429 and not last:
                ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                wait = int(ra) if (ra and str(ra).isdigit()) else 12
                wait = min(max(wait, 3), 65) + 1
                print(f"  ⚠️  HTTP 429 (rate limit), espero {wait}s… ({attempt+1}/{retries})")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                rw = _rate_wait_from_errors(data)
                if rw is not None and not last:
                    rw = min(max(rw, 3), 65) + 1
                    print(f"  ⚠️  Límite de complejidad, espero {rw}s… ({attempt+1}/{retries})")
                    time.sleep(rw)
                    continue
                print(f"  ⚠️  GraphQL errors: {data['errors']}")
                return None
            return data.get("data")
        except requests.exceptions.HTTPError as e:
            sc = getattr(resp, "status_code", None)
            if sc in (500, 502, 503, 504) and not last:
                wait = min((attempt + 1) * 10, 45)
                print(f"  ⚠️  HTTP {sc}, reintento en {wait}s… ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                print(f"  ❌ HTTP error: {e}")
                raise
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as e:
            if not last:
                wait = (attempt + 1) * 8
                print(f"  ⚠️  Error de conexión, reintento en {wait}s… ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                print(f"  ❌ Error de conexión tras {retries} intentos: {e}")
                raise


def _rules_to_gql(rules):
    """Convierte una lista de reglas a la cláusula query_params de Monday."""
    parts = []
    for r in rules:
        cv = json.dumps(r.get("compare_value", []))   # JSON array = lista GraphQL válida
        parts.append('{ column_id: "%s", compare_value: %s, operator: %s }'
                     % (r["column_id"], cv, r["operator"]))
    return ", query_params: { rules: [ %s ] }" % ", ".join(parts)


def fetch_all_items(board_id, columns_ids, rules=None, need_value=True):
    """Obtiene items de un board con paginación por cursor.
    - rules: filtro server-side (query_params) → trae solo los que cumplen.
    - need_value=False: omite el campo 'value' (lectura más ligera / menos rate-limit)."""
    # 2026-08-18 · Se pide SIEMPRE el campo tipado de board_relation. Bajo
    # API-Version 2024-10 el `value` de una board_relation no trae linkedPulseIds,
    # así que is_relation_set() daba False para TODAS las filas y el linker
    # reescribía la relación de 18.141 encuestas cada noche ("Ya con relación: 0"
    # con el board entero ya enlazado). Con linked_item_ids la guarda funciona.
    _rel = " ... on BoardRelationValue { linked_item_ids }"
    cv_fields = ("id text value" if need_value else "id text") + _rel
    qp = _rules_to_gql(rules) if rules else ""
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
                    items { id name column_values(ids: %s) { %s } }
                }
            }
            """ % (json.dumps(columns_ids), cv_fields)
            variables = {"cursor": cursor}
        else:
            query = """
            query ($boardId: [ID!]!) {
                boards(ids: $boardId) {
                    items_page(limit: 500%s) {
                        cursor
                        items { id name column_values(ids: %s) { %s } }
                    }
                }
            }
            """ % (qp, json.dumps(columns_ids), cv_fields)
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


def set_columns_batch(board_id, updates, batch_size=15, max_seconds=None):
    """Escribe varias columnas de varios ítems en lotes (mutaciones con alias en
    una sola petición → ~batch_size× menos llamadas). updates: lista de
    (item_id, col_values_dict). Si un lote falla, cae a 1×1 para no perder nada.
    max_seconds: v60eu · presupuesto de tiempo. Si se supera, se para y devuelve lo
    escrito (el resto queda para la próxima ejecución — es idempotente). Evita que
    un backlog grande haga que GitHub Actions cancele el job por exceder el límite."""
    written = 0
    total = len(updates)
    _t0 = time.time()
    for i in range(0, total, batch_size):
        if max_seconds and (time.time() - _t0) > max_seconds:
            print(f"    ⏱️  Tope de tiempo ({max_seconds}s): paro en {i}/{total}. "
                  f"El resto ({total - i}) se enlazará en la próxima ejecución.")
            break
        chunk = updates[i:i + batch_size]
        var_defs = ["$board: ID!"]
        variables = {"board": str(board_id)}
        muts = []
        for j, (iid, cv) in enumerate(chunk):
            var_defs.append("$v%d: JSON!" % j)
            variables["v%d" % j] = json.dumps(cv)
            muts.append("m%d: change_multiple_column_values(board_id: $board, item_id: %d, column_values: $v%d) { id }"
                        % (j, int(iid), j))
        query = "mutation (%s) { %s }" % (", ".join(var_defs), " ".join(muts))
        try:
            if monday_query(query, variables):
                written += len(chunk)
        except Exception as e:
            print(f"    ⚠️  Lote falló ({type(e).__name__}); reintento 1×1…")
            for iid, cv in chunk:
                try:
                    if set_columns(board_id, iid, cv):
                        written += 1
                except Exception:
                    pass
        print(f"    … {min(i + batch_size, total)}/{total} procesados ({written} escritos)")
    return written


def is_relation_set(item, col_id):
    """Devuelve True si la columna board_relation ya tiene un item vinculado.

    2026-08-18 · Prioriza `linked_item_ids` (campo tipado de BoardRelationValue).
    Bajo API-Version 2024-10 el `value` NO trae linkedPulseIds, así que el parseo
    del JSON devolvía False siempre → el linker reescribía todo el board cada
    noche. Se mantiene el parseo de `value` como respaldo (y se aceptan las dos
    grafías de la clave) para no depender de una sola vía."""
    for cv in item.get("column_values", []):
        if cv["id"] == col_id:
            lids = cv.get("linked_item_ids")
            if isinstance(lids, list) and len(lids) > 0:
                return True
            val = cv.get("value")
            if not val:
                return bool(lids)
            try:
                parsed = json.loads(val)
                linked = parsed.get("linkedPulseIds") or parsed.get("linked_pulse_ids") or []
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
    parser.add_argument("--board", choices=["cursos", "egh", "subvenciones", "ambos"], default="ambos",
                        help="Board(s) a procesar (default: ambos = egh + cursos + subvenciones)")
    parser.add_argument("--force", action="store_true",
                        help="Sobrescribir aunque ya tenga valor")
    args = parser.parse_args()

    print(f"{'🔍 DRY RUN — ' if args.dry_run else ''}Vinculando Alumno (rel) + Empresa en Encuestas")
    if args.force:
        print("⚠️  FORCE: se sobrescribirán valores existentes")
    print("=" * 70)

    # ── Ruta optimizada para --board cursos / subvenciones ───────────────────
    # Procesa SOLO los ítems sin enlazar (no relee los dos boards enteros). Si el
    # filtrado server-side fallara por lo que sea, cae al método completo de abajo.
    # Subvenciones es un clon del board Cursos → misma ruta/columnas.
    if args.board in ("cursos", "subvenciones") and not args.force:
        _fast_board = ENCUESTAS_CURSOS if args.board == "cursos" else ENCUESTAS_SUBV
        _fast_label = "Cursos" if args.board == "cursos" else "Subvenciones"
        try:
            _process_cursos_fast(args, board_id=_fast_board, board_label=_fast_label)
            return
        except Exception as e:
            print(f"⚠️  Ruta optimizada falló ({type(e).__name__}: {e}); uso método completo…")

    # ── 1. Cargar items del board Alumnos (DNI + Empresa) ──
    print(f"\n📥 Cargando Alumnos ({ALUMNOS_BOARD}) con DNI y Empresa...")
    alumnos_items = fetch_all_items(ALUMNOS_BOARD, [ALUMNOS_DNI_COL, ALUMNOS_EMPRESA_COL], need_value=False)
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
    if args.board in ("subvenciones", "ambos"):
        _process_cursos(args, dni_to_alumno_id, dni_to_empresa,
                        board_id=ENCUESTAS_SUBV, board_label="Subvenciones")
    if args.board in ("egh", "ambos"):
        _process_egh(args, dni_to_empresa)


def _process_cursos_fast(args, board_id=ENCUESTAS_CURSOS, board_label="Cursos"):
    """Ruta optimizada (--board cursos / subvenciones): procesa SOLO los ítems sin
    'Alumno (rel)' y consulta SOLO los Alumnos de esos DNIs. Evita releer los
    dos boards enteros cada noche (de ahí que tardara >1h). Si algo del filtrado
    fallara, main() cae al método completo. Subvenciones usa las MISMAS columnas
    que Cursos (clon estructural) → mismo código, solo cambia el board_id."""
    print(f"\n{'─' * 70}")
    print(f"📋 Encuestas: {board_label} ({board_id}) · solo ítems sin enlazar")

    # 1) Solo ítems con "Alumno (rel)" vacío (filtro server-side).
    rules = [{"column_id": CURSOS_RELATION_COL, "compare_value": [], "operator": "is_empty"}]
    enc_items = fetch_all_items(board_id,
                                [CURSOS_DNI_COL, CURSOS_EMPRESA_COL, CURSOS_RELATION_COL],
                                rules=rules)
    pend, no_dni, ya_rel = [], 0, 0
    for it in enc_items:
        # v60eu · GUARDA CLIENTE: si el filtro server-side is_empty NO filtró
        # (bajo API 2024-10 devolvía el board ENTERO → re-escribía los ~35k cada
        # noche → >5h → GitHub Actions lo cancelaba), NO reescribimos los ítems que
        # YA tienen 'Alumno (rel)'. Solo procesamos los realmente vacíos.
        if is_relation_set(it, CURSOS_RELATION_COL):
            ya_rel += 1
            continue
        dni = normalize_dni(get_column_value(it, CURSOS_DNI_COL))
        if dni:
            pend.append((it, dni))
        else:
            no_dni += 1
    print(f"   Recibidos: {len(enc_items)} · ya enlazados (saltados): {ya_rel} · "
          f"sin enlazar con DNI: {len(pend)} · sin DNI: {no_dni}")
    if not pend:
        print("   ✓ No hay ítems nuevos que enlazar.")
        return

    # 2) Mapa DNI → alumno desde Alumnos (lectura completa pero LIGERA, sin
    #    'value'). Se mantiene el match normalizado en Python (quita guiones/
    #    puntos, mayúsculas) para no perder DNIs con formato distinto entre boards.
    print(f"   📥 Cargando Alumnos ({ALUMNOS_BOARD})…")
    alumnos = fetch_all_items(ALUMNOS_BOARD, [ALUMNOS_DNI_COL, ALUMNOS_EMPRESA_COL], need_value=False)
    dni_to_alumno_id, dni_to_empresa = {}, {}
    for a in alumnos:
        d = normalize_dni(get_column_value(a, ALUMNOS_DNI_COL))
        if not d:
            continue
        dni_to_alumno_id[d] = a["id"]
        e = get_column_value(a, ALUMNOS_EMPRESA_COL)
        if e:
            dni_to_empresa[d] = e
    print(f"   Alumnos con DNI: {len(dni_to_alumno_id)}")

    # 3) Construir y escribir (en lotes) Alumno (rel) + Empresa - dashboard.
    updates, not_found = [], 0
    for it, dni in pend:
        aid = dni_to_alumno_id.get(dni)
        if not aid:
            not_found += 1
            continue
        cv = {CURSOS_RELATION_COL: {"item_ids": [int(aid)]}}
        emp = dni_to_empresa.get(dni)
        if emp and not get_column_value(it, CURSOS_EMPRESA_COL):
            cv[CURSOS_EMPRESA_COL] = emp
        updates.append((it["id"], cv))
    print(f"   A escribir: {len(updates)} · DNI sin alumno en Alumnos: {not_found}")

    if args.dry_run:
        for it, dni in pend[:20]:
            print(f"   [dry-run] {it['name'][:60]} | dni={dni}")
        return
    if not updates:
        print("   ✓ Nada que escribir.")
        return

    # v60eu · Presupuesto de tiempo (env LINK_MAX_SECONDS, por defecto 3h) para no
    # agotar el límite de GitHub Actions con un backlog grande.
    _budget = int(os.environ.get("LINK_MAX_SECONDS", "10800"))
    written = set_columns_batch(board_id, updates, max_seconds=_budget)
    print(f"   ✅ Escritos {written}/{len(updates)} ítems (Alumno rel + Empresa)")


def _process_cursos(args, dni_to_alumno_id, dni_to_empresa,
                    board_id=ENCUESTAS_CURSOS, board_label="Cursos"):
    """Cursos / Subvenciones: rellena Alumno (rel) + Empresa - dashboard.
    Subvenciones es un clon estructural de Cursos → mismas columnas, solo cambia
    el board_id."""
    print(f"\n{'─' * 70}")
    print(f"📋 Encuestas: {board_label} ({board_id})")
    print(f"   Columnas: Alumno (rel) [{CURSOS_RELATION_COL}] + Empresa [{CURSOS_EMPRESA_COL}]")

    cols = [CURSOS_DNI_COL, CURSOS_EMPRESA_COL, CURSOS_RELATION_COL]
    enc_items = fetch_all_items(board_id, cols)
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
            result = set_columns(board_id, item["id"], col_values)
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

    print(f"\n   📊 RESUMEN {board_label} {'(DRY RUN)' if args.dry_run else ''}:")
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
