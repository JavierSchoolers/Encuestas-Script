"""
evolcampus_monday_sync_cursos.py
─────────────────────────────────────────────────────────────────────────────
Integración automática: EvolCampus API → Monday.com
Script para cursos NO-EGH/CSUL

Modos de uso:
  python3 evolcampus_monday_sync_cursos.py --setup        # Crear board en Monday (solo 1ª vez)
  python3 evolcampus_monday_sync_cursos.py --sync         # Sincronizar datos
  python3 evolcampus_monday_sync_cursos.py --sync --dry-run
  python3 evolcampus_monday_sync_cursos.py --explore      # Ver estructura de un grupo (solo lectura)

Requisitos:
  pip install requests
─────────────────────────────────────────────────────────────────────────────
"""

import requests
import json
import sys
import os
import time
import subprocess
import argparse
import re
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

# Credenciales: SIEMPRE desde variables de entorno / GitHub Secrets (nunca en código).
EVOLCAMPUS_CLIENT_ID = os.environ.get("EVOLCAMPUS_CLIENT_ID", "")
EVOLCAMPUS_KEY       = os.environ.get("EVOLCAMPUS_KEY", "")
EVOLCAMPUS_API       = "https://api.evolcampus.com/api/v1"

MONDAY_TOKEN         = os.environ.get("MONDAY_TOKEN", "")
MONDAY_API           = "https://api.monday.com/v2"

# Aviso temprano si faltan credenciales (evita escrituras silenciosas con token vacío).
if not MONDAY_TOKEN or not EVOLCAMPUS_KEY or not EVOLCAMPUS_CLIENT_ID:
    import sys as _sys
    _faltan = [n for n, v in (("MONDAY_TOKEN", MONDAY_TOKEN),
                              ("EVOLCAMPUS_KEY", EVOLCAMPUS_KEY),
                              ("EVOLCAMPUS_CLIENT_ID", EVOLCAMPUS_CLIENT_ID)) if not v]
    print(f"✗ Faltan variables de entorno: {', '.join(_faltan)}. "
          f"Configúralas como Secrets del repo (Settings → Secrets and variables → Actions).",
          file=_sys.stderr)
    _sys.exit(2)
MONDAY_BOARD_NAME    = "Encuestas: Cursos"

CONFIG_FILE          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monday_config_cursos.json")

# Palabras clave para detectar categoría de pregunta
FORMADOR_KW  = ["formador", "ponente", "docente", "profesor", "tutor", "trainer"]
CONTENIDO_KW = ["contenido", "materiales", "temario", "información", "aprendizaje", "conocimiento"]
FORMATO_KW   = ["formato", "plataforma", "herramienta", "metodología", "dinámica", "organización", "estructura"]

# ── Nombres alternativos por course_id (cuando la API devuelve el mismo nombre para varios cursos) ──
COURSE_NAME_OVERRIDES = {
    489:  "Programa Avanzado de Alimentos y Bebidas (2023)",
    780:  "Programa Avanzado de Alimentos y Bebidas (2025)",
    1118: "Programa Avanzado de Alimentos y Bebidas (Actualizado)",
    729:  "Programa Avanzado de Liderazgo Hotelero (40 horas)",
    1103: "Programa Avanzado de Liderazgo Hotelero (66 horas)",
}

# ── Cursos EXCLUIDOS (parcial, case-insensitive) ──────────────────────────────
# Todo lo que no esté aquí se sincroniza (si el grupo pasa los filtros)
EXCLUDED_COURSES = [
    "EGH", "Executive Global Hospitality", "Gestión Responsable",  # EGH → su propio script
    "EISI HOTEL", "Bienvenidos", "Examples",                        # Demos/admin
    "EISI WORK",                                                    # Gestión interna
    "MLL SOP",                                                      # SOP's cocina
    "Patrón de Embarcaciones",                                      # PER
]

# ── Palabras clave que excluyen un GRUPO concreto ────────────────────────────
EXCLUDED_GROUP_KW = [
    "matrícula abierta", "matricula abierta",
    "grupo abierto", "abierto 2025", "abierta 2025",
]

# ── Encuestas excluidas (por nombre de survey, parcial, case-insensitive) ────
# Se tratan aparte; no deben entrar en el dashboard de módulos
EXCLUDED_SURVEY_NAMES = [
    # Encuestas de programa (ya excluidas antes)
    "qué opinas de nuestro programa",
    "que opinas de nuestro programa",
    "qué te ha parecido nuestro curso",
    "que te ha parecido nuestro curso",
    # Encuestas genéricas no deseadas
    "formación bonificada",
    "finalización del programa",
    "satisfacción - 50%",
    "hotelatelier",
    "what do you think about our course",
    "what do you think of the program",
    "ha ido el examen",
    "opinas de nuestro curso",
    "ha parecido el curso",
    "ha parecido el programa",
    "primera parte del programa",
]


# ══════════════════════════════════════════════════════════════════════════════
# EVOLCAMPUS API
# ══════════════════════════════════════════════════════════════════════════════

_evol_token = None

def evol_token():
    global _evol_token
    if _evol_token:
        return _evol_token
    resp = requests.post(f"{EVOLCAMPUS_API}/token",
                         json={"clientid": EVOLCAMPUS_CLIENT_ID, "key": EVOLCAMPUS_KEY},
                         timeout=15)
    resp.raise_for_status()
    _evol_token = resp.json().get("token")
    if not _evol_token:
        raise ValueError("No se obtuvo token de EvolCampus")
    print("  ✓ Token EvolCampus obtenido")
    return _evol_token


def get_all_groups():
    headers = {"Authorization": f"Bearer {evol_token()}"}
    resp = requests.get(f"{EVOLCAMPUS_API}/getCoursesGroups", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    groups = []
    items = data if isinstance(data, list) else data.get("courses", data.get("groups", data.get("data", [])))
    for item in items:
        course_id   = item.get("id")
        course_name = COURSE_NAME_OVERRIDES.get(course_id) or item.get("name", "")
        for group in item.get("groups", []):
            groups.append({
                "course":     course_name,
                "group_id":   group.get("groupid"),
                "group_name": group.get("group", ""),
            })
    return groups


def get_surveys_by_group(group_id):
    """Obtiene encuestas de un grupo, siempre con activities=True."""
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        gid = group_id
    headers = {"Authorization": f"Bearer {evol_token()}"}
    payload = {"groupid": gid, "activities": True}
    resp = requests.post(f"{EVOLCAMPUS_API}/getSurveysByGroup",
                         headers=headers, json=payload, timeout=30)
    if resp.status_code == 400:
        return None
    if not resp.ok:
        print(f"    Detalle error API: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# PROCESAMIENTO DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

def strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text or "").strip()


def categorize_question(question_text):
    text = strip_html(question_text).lower()
    for kw in FORMADOR_KW:
        if kw in text:
            return "formador"
    for kw in CONTENIDO_KW:
        if kw in text:
            return "contenido"
    for kw in FORMATO_KW:
        if kw in text:
            return "formato"
    return "otro"


def natural_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]


def extract_formador_name(question_html):
    text = strip_html(question_html)
    m = re.search(r'(?:formador[a]?|ponente|docente|profesor[a]?)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)*)', text)
    return m.group(1).strip() if m else None


def to_pct(scores):
    """Promedia una lista de puntuaciones ya normalizadas a porcentaje 0-100."""
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def detect_survey_scale(survey):
    """Detecta la escala (4 o 5) de una encuesta mirando todos los value_answer."""
    for record in survey.get("records", []):
        for q in record.get("questions", []):
            if q.get("type", -1) == 0:
                val = (q.get("learner_answer") or {}).get("value_answer")
                if val is not None:
                    try:
                        if float(val) > 4:
                            return 5
                    except (ValueError, TypeError):
                        pass
    return 4


def val_to_pct(val, scale):
    """Convierte un valor raw a porcentaje según la escala."""
    return round(val / scale * 100, 1)


def parse_group_dates(group_name):
    start_date = ""
    end_date = ""
    m = re.search(r'(\d{1,2}/\d{2}(?:/\d{4})?)\s*(?:al?|-)\s*(\d{1,2}/\d{2}(?:/\d{4})?)', group_name)
    if m:
        raw_start, raw_end = m.group(1), m.group(2)
        year_match = re.search(r'(\d{4})', group_name)
        year = year_match.group(1) if year_match else str(datetime.now().year)
        for raw, target in [(raw_start, "start"), (raw_end, "end")]:
            parts = raw.split("/")
            if len(parts) == 2:
                d, mo = parts
                y = year
            else:
                d, mo, y = parts
                if len(y) == 2:
                    y = "20" + y
            try:
                dt = datetime(int(y), int(mo), int(d))
                if target == "start":
                    start_date = dt.strftime("%Y-%m-%d")
                else:
                    end_date = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
    return start_date, end_date


def process_group_data(raw_data, group_name, course_name, allowed_dnis=None):
    """
    Procesa la respuesta cruda de getSurveysByGroup para cursos NO-EGH.
    El nombre del módulo viene de survey["subject"].

    Si allowed_dnis (set de DNIs normalizados) está definido, SOLO se tienen en
    cuenta los registros de esos alumnos (export filtrado por empresa). Las medias
    de módulo/programa se recalculan únicamente con esos alumnos.
    """
    grp = raw_data.get("group", raw_data)
    surveys = grp.get("surveys", [])
    course_name = grp.get("course") or course_name
    group_name  = grp.get("name") or group_name

    program_scores_global    = []
    program_scores_formador  = []
    program_scores_contenido = []
    program_scores_formato   = []
    program_n_responses      = set()
    program_n_comments       = 0
    program_last_date        = None

    module_rows     = []
    individual_rows = []

    for survey in surveys:
        if survey.get("type", "").lower() == "fundae":
            continue

        # Excluir encuestas de programa global (se tratan aparte)
        _sname = (survey.get("name") or "").strip().lower()
        if any(kw in _sname for kw in EXCLUDED_SURVEY_NAMES):
            continue

        # Detectar escala de esta encuesta (4 o 5) antes de procesar
        survey_scale = detect_survey_scale(survey)

        # Nombre del módulo: campo "subject" (asignatura) de la API
        module_name = (survey.get("subject") or survey.get("name") or "Sin nombre")
        module_name = re.sub(r':\s*encuest[as]+\s*$', '', module_name, flags=re.IGNORECASE).strip()

        records = survey.get("records", [])

        survey_date = None
        for date_key in ("date", "fecha", "submission_date", "answered_at", "created_at", "end_date"):
            sdate = survey.get(date_key)
            if sdate:
                try:
                    survey_date = datetime.fromisoformat(str(sdate)[:19])
                except Exception:
                    pass
                if survey_date:
                    break

        mod_scores_global    = []
        mod_scores_formador  = []
        mod_scores_contenido = []
        mod_scores_formato   = []
        mod_formador_scores  = {}
        mod_n_responses      = 0
        mod_n_comments       = 0
        mod_comments         = []
        mod_last_date        = None
        mod_individual_rows  = []

        for record in records:
            student   = record.get("name") or "Anónimo"
            student_dni = record.get("id_card") or ""
            # Export filtrado por empresa: saltar alumnos que no pertenecen a la marca.
            if allowed_dnis is not None:
                _dnin = re.sub(r'[^0-9A-Za-z]', '', str(student_dni)).upper()
                if not _dnin or _dnin not in allowed_dnis:
                    continue
            questions = record.get("questions", [])
            has_answer = False

            rec_scores_global    = []
            rec_scores_formador  = []
            rec_scores_contenido = []
            rec_scores_formato   = []
            rec_formador_scores  = {}
            rec_comments         = []
            rec_last_date        = None

            for date_key in ("date", "fecha", "submission_date", "answered_at", "created_at"):
                rdate = record.get(date_key)
                if rdate:
                    try:
                        d = datetime.fromisoformat(str(rdate)[:19])
                        rec_last_date = d
                        if mod_last_date is None or d > mod_last_date:
                            mod_last_date = d
                        if program_last_date is None or d > program_last_date:
                            program_last_date = d
                    except Exception:
                        pass
                    break

            for q in questions:
                qtype  = q.get("type", -1)
                qtext  = q.get("question", "")
                answer = q.get("learner_answer") or {}

                if rec_last_date is None:
                    for date_key in ("date", "answered_at", "created_at"):
                        qdate = answer.get(date_key)
                        if qdate:
                            try:
                                d = datetime.fromisoformat(str(qdate)[:19])
                                rec_last_date = d
                                if mod_last_date is None or d > mod_last_date:
                                    mod_last_date = d
                                if program_last_date is None or d > program_last_date:
                                    program_last_date = d
                            except Exception:
                                pass
                            break

                if qtype == 0:
                    val = answer.get("value_answer")
                    if val is not None:
                        try:
                            val = float(val)
                            pct = val_to_pct(val, survey_scale)
                            cat = categorize_question(qtext)
                            rec_scores_global.append(pct)
                            mod_scores_global.append(pct)
                            program_scores_global.append(pct)
                            if cat == "formador":
                                rec_scores_formador.append(pct)
                                mod_scores_formador.append(pct)
                                program_scores_formador.append(pct)
                                fname = extract_formador_name(qtext)
                                if fname:
                                    mod_formador_scores.setdefault(fname, []).append(pct)
                                    rec_formador_scores.setdefault(fname, []).append(pct)
                            elif cat == "contenido":
                                rec_scores_contenido.append(pct)
                                mod_scores_contenido.append(pct)
                                program_scores_contenido.append(pct)
                            elif cat == "formato":
                                rec_scores_formato.append(pct)
                                mod_scores_formato.append(pct)
                                program_scores_formato.append(pct)
                            has_answer = True
                        except (ValueError, TypeError):
                            pass

                if qtype == 1:
                    text_ans = answer.get("text_answer", "")
                    if text_ans and str(text_ans).strip():
                        rec_comments.append(str(text_ans).strip())
                        mod_n_comments += 1
                        program_n_comments += 1
                        mod_comments.append({"text": str(text_ans).strip(), "student": student, "date": rec_last_date.strftime("%Y-%m-%d") if rec_last_date else ""})

            if has_answer:
                mod_n_responses += 1
                program_n_responses.add(student)
                rec_formadores_ord = sorted(rec_formador_scores.keys())
                rec_row = {
                    "curso":          course_name,
                    "grupo":          group_name,
                    "modulo":         module_name,
                    "alumno":         student,
                    "dni":            student_dni,
                    "formador":       " / ".join(rec_formadores_ord),
                    "pct_global":     to_pct(rec_scores_global),
                    "pct_formador":   to_pct(rec_scores_formador),
                    "pct_contenido":  to_pct(rec_scores_contenido),
                    "pct_formato":    to_pct(rec_scores_formato),
                    "n_comentarios":  len(rec_comments),
                    "comentarios":    json.dumps([{"text": c, "student": student, "date": rec_last_date.strftime("%Y-%m-%d") if rec_last_date else ""} for c in rec_comments], ensure_ascii=False) if rec_comments else "",
                    "fecha_ultima":    rec_last_date.strftime("%Y-%m-%d") if rec_last_date else "",
                    "fecha_ultima_dt": rec_last_date.strftime("%Y-%m-%dT%H:%M:%S") if rec_last_date else "",
                    "fecha_encuesta":  rec_last_date.strftime("%Y-%m-%d") if rec_last_date else "",
                }
                for i, fname in enumerate(rec_formadores_ord[:6], 1):
                    rec_row[f"formador_{i}"]     = fname
                    rec_row[f"pct_formador_{i}"] = to_pct(rec_formador_scores[fname])
                mod_individual_rows.append(rec_row)

        _fecha_fallback = mod_last_date or survey_date
        if _fecha_fallback:
            for ir in mod_individual_rows:
                if not ir.get("fecha_encuesta"):
                    ir["fecha_encuesta"]  = _fecha_fallback.strftime("%Y-%m-%d")
                if not ir.get("fecha_ultima"):
                    ir["fecha_ultima"]    = _fecha_fallback.strftime("%Y-%m-%d")
                if not ir.get("fecha_ultima_dt"):
                    ir["fecha_ultima_dt"] = _fecha_fallback.strftime("%Y-%m-%dT%H:%M:%S")
            if mod_last_date is None:
                mod_last_date = _fecha_fallback

        individual_rows.extend(mod_individual_rows)

        formadores_ordenados = sorted(mod_formador_scores.keys())
        pct_por_formador = [to_pct(mod_formador_scores[f]) for f in formadores_ordenados]
        pct_formador_overall = (
            round(sum(p for p in pct_por_formador if p is not None) /
                  len([p for p in pct_por_formador if p is not None]), 1)
            if pct_por_formador and any(p is not None for p in pct_por_formador)
            else to_pct(mod_scores_formador)
        )

        mod_row = {
            "curso":          course_name,
            "grupo":          group_name,
            "modulo":         module_name,
            "formador":       " / ".join(formadores_ordenados),
            "pct_global":     to_pct(mod_scores_global),
            "respuestas":     mod_n_responses,
            "pct_formador":   pct_formador_overall,
            "pct_contenido":  to_pct(mod_scores_contenido),
            "pct_formato":    to_pct(mod_scores_formato),
            "n_comentarios":  mod_n_comments,
            "comentarios":    json.dumps(mod_comments, ensure_ascii=False) if mod_comments else "",
            "fecha_ultima":    mod_last_date.strftime("%Y-%m-%d") if mod_last_date else "",
            "fecha_ultima_dt": mod_last_date.strftime("%Y-%m-%dT%H:%M:%S") if mod_last_date else "",
        }
        for i, fname in enumerate(formadores_ordenados[:6], 1):
            mod_row[f"formador_{i}"]     = fname
            mod_row[f"pct_formador_{i}"] = to_pct(mod_formador_scores[fname])

        module_rows.append(mod_row)

    module_rows.sort(key=lambda x: natural_key(x["modulo"]))

    start_date, end_date = parse_group_dates(group_name)

    program_row = {
        "curso":          course_name,
        "grupo":          group_name,
        "modulo":         "(Programa completo)",
        "formador":       "",
        "pct_global":     to_pct(program_scores_global),
        "respuestas":     len(program_n_responses),
        "pct_formador":   to_pct(program_scores_formador),
        "pct_contenido":  to_pct(program_scores_contenido),
        "pct_formato":    to_pct(program_scores_formato),
        "n_comentarios":  program_n_comments,
        "fecha_ultima":    program_last_date.strftime("%Y-%m-%d") if program_last_date else "",
        "fecha_ultima_dt": program_last_date.strftime("%Y-%m-%dT%H:%M:%S") if program_last_date else "",
        "fecha_inicio":    start_date,
        "fecha_fin":       end_date,
    }

    return program_row, module_rows, individual_rows


# ══════════════════════════════════════════════════════════════════════════════
# MONDAY.COM API
# ══════════════════════════════════════════════════════════════════════════════

def monday_query(query, variables=None, retries=4):
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    # Reintentos ante timeouts / errores transitorios de red o servidor (429/5xx).
    # El board Cursos tiene ~18k ítems y la precarga a veces excede el timeout.
    for attempt in range(retries):
        try:
            resp = requests.post(MONDAY_API, headers=headers, json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise ValueError(f"Monday API error: {data['errors']}")
            return data.get("data", {})
        except requests.exceptions.HTTPError as e:
            sc = getattr(e.response, "status_code", None)
            if sc in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = (attempt + 1) * 15
                print(f"  ⚠️  HTTP {sc}, reintentando en {wait}s... ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.RequestException as e:
            # Timeout, ConnectionError, ChunkedEncodingError, ProtocolError… → transitorios
            if attempt < retries - 1:
                wait = (attempt + 1) * 15
                print(f"  ⚠️  Red ({type(e).__name__}), reintentando en {wait}s... ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise


def monday_setup_board():
    """Crea el board 'Encuestas: Cursos' con secciones y columnas."""
    q_create = """
    mutation($name: String!) {
      create_board(board_name: $name, board_kind: public) {
        id
      }
    }
    """
    data = monday_query(q_create, {"name": MONDAY_BOARD_NAME})
    board_id = data["create_board"]["id"]
    print(f"  ✓ Board creado: ID {board_id}")

    columns = [
        ("grupo",          "Grupo",                  "text"),
        ("modulo",         "Módulo",                 "text"),
        ("formador",       "Formador",               "text"),
        ("alumno",         "Alumno",                 "text"),
        ("pct_global",     "% Global",               "numbers"),
        ("respuestas",     "Respuestas",             "numbers"),
        ("pct_formador",   "% Formador",             "numbers"),
        ("pct_contenido",  "% Contenido",            "numbers"),
        ("pct_formato",    "% Formato",              "numbers"),
        ("n_comentarios",  "Nº Comentarios",         "numbers"),
        ("fecha_ultima",   "Fecha última encuesta",  "date"),
        ("comentarios",    "Comentarios",            "long_text"),
        ("fecha_inicio",   "Fecha inicio",           "date"),
        ("fecha_fin",      "Fecha fin",              "date"),
        ("fecha_encuesta", "Fecha encuesta",         "date"),
        ("formador_1",     "Formador 1",             "text"),
        ("pct_formador_1", "% Formador 1",           "numbers"),
        ("formador_2",     "Formador 2",             "text"),
        ("pct_formador_2", "% Formador 2",           "numbers"),
        ("formador_3",     "Formador 3",             "text"),
        ("pct_formador_3", "% Formador 3",           "numbers"),
        ("formador_4",     "Formador 4",             "text"),
        ("pct_formador_4", "% Formador 4",           "numbers"),
        ("formador_5",     "Formador 5",             "text"),
        ("pct_formador_5", "% Formador 5",           "numbers"),
        ("formador_6",     "Formador 6",             "text"),
        ("pct_formador_6", "% Formador 6",           "numbers"),
    ]

    col_ids = {}
    q_col = """
    mutation($board_id: ID!, $title: String!, $type: ColumnType!) {
      create_column(board_id: $board_id, title: $title, column_type: $type) {
        id
        title
      }
    }
    """
    for col_key, col_title, col_type in columns:
        d = monday_query(q_col, {"board_id": board_id, "title": col_title, "type": col_type})
        col_ids[col_key] = d["create_column"]["id"]
        print(f"    + Columna '{col_title}' → {col_ids[col_key]}")

    q_group = """
    mutation($board_id: ID!, $name: String!) {
      create_group(board_id: $board_id, group_name: $name) {
        id
      }
    }
    """
    d1 = monday_query(q_group, {"board_id": board_id, "name": "Por Programa"})
    d2 = monday_query(q_group, {"board_id": board_id, "name": "Por Módulo"})
    d3 = monday_query(q_group, {"board_id": board_id, "name": "Por Encuesta"})
    group_programa_id = d1["create_group"]["id"]
    group_modulo_id   = d2["create_group"]["id"]
    group_encuesta_id = d3["create_group"]["id"]
    print(f"  ✓ Secciones: Por Programa ({group_programa_id}), Por Módulo ({group_modulo_id}), Por Encuesta ({group_encuesta_id})")

    config = {
        "board_id":          board_id,
        "group_programa_id": group_programa_id,
        "group_modulo_id":   group_modulo_id,
        "group_encuesta_id": group_encuesta_id,
        "col_ids":           col_ids,
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  ✓ Configuración guardada en {CONFIG_FILE}")
    return config


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"No se encontró {CONFIG_FILE}. Ejecuta primero: python3 evolcampus_monday_sync_cursos.py --setup"
        )


def preload_board_items(board_id):
    """Pre-carga todos los items del board en un dict {name: item_id}.
    Así evitamos hacer una búsqueda por nombre en cada upsert (ahorra ~50% de llamadas API)."""
    print(f"\n  📦 Pre-cargando items existentes del board...")
    items_map = {}
    cursor = None
    page = 0

    while True:
        page += 1
        if cursor:
            query = """
            query($cursor: String!) {
                next_items_page(cursor: $cursor, limit: 500) {
                    cursor
                    items { id name }
                }
            }
            """
            data = monday_query(query, {"cursor": cursor})
            page_data = data.get("next_items_page", {})
        else:
            query = """
            query($boardId: [ID!]!) {
                boards(ids: $boardId) {
                    items_page(limit: 500) {
                        cursor
                        items { id name }
                    }
                }
            }
            """
            data = monday_query(query, {"boardId": [str(board_id)]})
            boards = data.get("boards", [])
            if not boards:
                break
            page_data = boards[0].get("items_page", {})

        items = page_data.get("items", [])
        new_cursor = page_data.get("cursor")

        for item in items:
            items_map[item["name"]] = item["id"]

        print(f"    Página {page}: {len(items)} items (total acumulado: {len(items_map)})")

        if not new_cursor or not items:
            break
        cursor = new_cursor

    print(f"  ✓ {len(items_map)} items pre-cargados en memoria\n")
    return items_map


def preload_program_dates(board_id, group_programa_id, fecha_col_id, fecha_dt_col_id=""):
    """Pre-carga la fecha/datetime de última encuesta de cada ítem 'Por Programa'.
    Usa fecha_ultima_dt (texto con hora) si está disponible; si no, fecha_ultima (date).
    Devuelve {item_name: datetime_or_date_str}."""
    cols = [c for c in [fecha_dt_col_id, fecha_col_id] if c]
    if not cols:
        return {}
    query = """
    query($boardId: [ID!]!, $groupId: [String!]!, $cols: [String!]!) {
        boards(ids: $boardId) {
            groups(ids: $groupId) {
                items_page(limit: 500) {
                    items {
                        name
                        column_values(ids: $cols) { id text }
                    }
                }
            }
        }
    }
    """
    data = monday_query(query, {
        "boardId": [str(board_id)],
        "groupId": [group_programa_id],
        "cols": cols,
    })
    items = (data.get("boards", [{}])[0]
                 .get("groups", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
    dates_map = {}
    for item in items:
        cv_by_id = {cv["id"]: cv["text"] for cv in item.get("column_values", [])}
        # Preferir datetime (con hora) sobre date (solo fecha)
        val = (cv_by_id.get(fecha_dt_col_id, "") or cv_by_id.get(fecha_col_id, ""))
        dates_map[item["name"]] = val
    print(f"  ✓ {len(dates_map)} fechas pre-cargadas de 'Por Programa'\n")
    return dates_map


def monday_upsert_item(board_id, group_id, item_name, col_ids, row, dry_run=False, items_cache=None):
    """Crea o actualiza un ítem en Monday.
    Si se pasa items_cache (dict {name: id}), se evita la búsqueda por nombre (más rápido)."""
    col_values = {}

    def add(key, val):
        if val is not None and key in col_ids:
            col_values[col_ids[key]] = str(val) if not isinstance(val, str) else val

    add("grupo",         row.get("grupo", ""))
    add("modulo",        row.get("modulo", ""))
    add("alumno",        row.get("alumno", ""))
    add("dni",           row.get("dni", ""))
    add("formador",      row.get("formador", ""))
    add("pct_global",    row.get("pct_global"))
    add("respuestas",    row.get("respuestas"))
    add("pct_formador",  row.get("pct_formador"))
    add("pct_contenido", row.get("pct_contenido"))
    add("pct_formato",   row.get("pct_formato"))
    add("n_comentarios", row.get("n_comentarios"))
    for i in range(1, 7):
        add(f"formador_{i}",     row.get(f"formador_{i}", ""))
        add(f"pct_formador_{i}", row.get(f"pct_formador_{i}"))

    comentarios_raw = row.get("comentarios", "")
    if comentarios_raw and "comentarios" in col_ids:
        col_values[col_ids["comentarios"]] = {"text": comentarios_raw}

    fecha = row.get("fecha_ultima", "")
    if fecha and "fecha_ultima" in col_ids:
        col_values[col_ids["fecha_ultima"]] = {"date": fecha}

    for fecha_key in ("fecha_inicio", "fecha_fin", "fecha_encuesta"):
        fval = row.get(fecha_key, "")
        if fval and fecha_key in col_ids:
            col_values[col_ids[fecha_key]] = {"date": fval}

    col_values_str = json.dumps(col_values)

    if dry_run:
        print(f"    [DRY-RUN] Ítem: {item_name}")
        return

    # Buscar si el item ya existe: primero en caché local, luego en Monday si no hay caché
    if items_cache is not None:
        item_id = items_cache.get(item_name)
    else:
        q_search = """
        query($board_id: ID!, $name: String!) {
          items_page_by_column_values(board_id: $board_id, limit: 5,
            columns: [{column_id: "name", column_values: [$name]}]) {
            items { id name }
          }
        }
        """
        try:
            result = monday_query(q_search, {"board_id": board_id, "name": item_name})
            found = result.get("items_page_by_column_values", {}).get("items", [])
            item_id = found[0]["id"] if found else None
        except Exception:
            item_id = None

    if item_id:
        q_update = """
        mutation($board_id: ID!, $item_id: ID!, $col_values: JSON!) {
          change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_values) {
            id
          }
        }
        """
        monday_query(q_update, {"board_id": board_id, "item_id": item_id, "col_values": col_values_str})
        print(f"    ↻ Actualizado: {item_name}")
    else:
        q_create = """
        mutation($board_id: ID!, $group_id: String!, $name: String!, $col_values: JSON!) {
          create_item(board_id: $board_id, group_id: $group_id,
                      item_name: $name, column_values: $col_values) {
            id
          }
        }
        """
        result = monday_query(q_create, {
            "board_id":   board_id,
            "group_id":   group_id,
            "name":       item_name,
            "col_values": col_values_str,
        })
        new_id = (result.get("create_item") or {}).get("id")
        if new_id and items_cache is not None:
            items_cache[item_name] = new_id  # actualizar caché para evitar duplicados en el mismo run
        print(f"    + Creado: {item_name}")


# ══════════════════════════════════════════════════════════════════════════════
# MODOS DE EJECUCIÓN
# ══════════════════════════════════════════════════════════════════════════════

def is_course_excluded(course):
    """Devuelve True si el curso debe omitirse."""
    c = course.strip().lower()
    return any(k.lower() in c for k in EXCLUDED_COURSES)


def is_group_excluded(group_name):
    """Devuelve True si el grupo debe omitirse (matrícula abierta, etc.)."""
    gn = group_name.lower()
    return any(k in gn for k in EXCLUDED_GROUP_KW)


def is_group_in_year(group_name, year):
    """Devuelve True si el grupo pertenece al año indicado."""
    return str(year) in group_name


def is_group_active(group_name):
    """Comprueba si un grupo está activo hoy: ya ha empezado Y aún no ha terminado.
    Si no hay año en el nombre, parse_group_dates asigna el año actual.
    Exigir start_date <= hoy evita activar grupos sin año cuyo inicio en el año
    actual es futuro (ej. grupos de 2024 cuya fecha, asignada a 2026, aún no ha llegado)."""
    start_date, end_date = parse_group_dates(group_name)
    if not end_date:
        return False
    # Rechazar grupos anteriores a 2025 (año extraído de la fecha parseada)
    if end_date[:4] < "2025":
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    # El grupo debe haber empezado ya (o no tener fecha de inicio)
    if start_date and start_date > today:
        return False
    return end_date >= today


def mode_explore():
    """Muestra la estructura de datos de un grupo (solo lectura)."""
    print("\n── EXPLORACIÓN ──────────────────────────────────────────")
    all_groups = get_all_groups()
    targets = [g for g in all_groups if is_course_allowed(g["course"])]

    if not targets:
        print("  No se encontraron grupos para los cursos permitidos.")
        return

    print(f"  {len(targets)} grupos encontrados")
    for g in targets:
        print(f"  - {g['course']} / {g['group_name']} (ID: {g['group_id']})")

    for g in targets:
        raw = get_surveys_by_group(g["group_id"])
        if raw is None:
            continue
        print(f"\n  Explorando: {g['course']} / {g['group_name']}")
        with open("explore_cursos_output.json", "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        print(f"  ✓ Guardado en explore_cursos_output.json")
        grp = raw.get("group", raw)
        for i, s in enumerate(grp.get("surveys", [])[:15]):
            stype = s.get("type", "?")
            subject = s.get("subject", "—sin subject—")
            recs = len(s.get("records", []))
            print(f"    [{i}] type={stype:8s} subject=\"{subject}\" records={recs}")
        break


def mode_sync(dry_run=False, year=None, filter_course=None, force=False):
    """Sincroniza cursos con Monday.
    Si year está definido, sincroniza todos los grupos de ese año (activos o no).
    Si no, solo sincroniza los grupos activos hoy.
    Si filter_course está definido, solo sincroniza grupos cuyo curso contenga ese texto.
    Si force=True, ignora el salto por fecha "sin cambios" y reprocesa todos los grupos
    (backfill: recoge respuestas tardías que no movieron la fecha máxima del programa).
    """
    year_label = f" año {year}" if year else " (grupos activos)"
    filter_label = f" · filtro: '{filter_course}'" if filter_course else ""
    print(f"\n── SYNC CURSOS{year_label}{filter_label} {'[DRY-RUN] ' if dry_run else ''}──────────────────────────────────")

    config = load_config()
    board_id           = config["board_id"]
    group_programa_id  = config["group_programa_id"]
    group_modulo_id    = config["group_modulo_id"]
    group_encuesta_id  = config.get("group_encuesta_id", "")
    col_ids            = config["col_ids"]

    # Pre-cargar items existentes del board para evitar búsquedas individuales.
    # Si se usa --filter-course se salta la precarga y se usan búsquedas individuales (None = busca en Monday cada vez).
    if not dry_run and not filter_course:
        items_cache = preload_board_items(board_id)
        fecha_col_id    = col_ids.get("fecha_ultima", "")
        fecha_dt_col_id = col_ids.get("fecha_ultima_dt", "")
        program_dates = preload_program_dates(board_id, group_programa_id, fecha_col_id, fecha_dt_col_id)
    elif not dry_run:
        items_cache = None   # búsqueda individual → evita duplicados sin necesidad de precargar todo el board
        program_dates = {}
    else:
        items_cache = {}
        program_dates = {}

    print("\n[1] Obteniendo grupos de EvolCampus...")
    all_groups = get_all_groups()
    print(f"  {len(all_groups)} grupos encontrados en total")

    synced = 0
    skipped_course = 0
    skipped_group  = 0
    failed_groups  = []

    for g in all_groups:
        group_id   = g["group_id"]
        group_name = g["group_name"]
        course     = g["course"]

        # Filtro de curso
        if is_course_excluded(course):
            skipped_course += 1
            continue

        # Filtro opcional por nombre de curso
        if filter_course and filter_course.lower() not in course.lower():
            continue

        # Filtro de grupo: matrícula abierta, etc.
        if is_group_excluded(group_name):
            skipped_group += 1
            continue

        # Filtro de año / actividad
        if year:
            if not is_group_in_year(group_name, year):
                continue
        else:
            if not is_group_active(group_name):
                continue

        course_clean = course.strip()
        print(f"\n  → {course_clean} / {group_name}")
        try:
            raw = get_surveys_by_group(group_id)
            if raw is None:
                print(f"  ⏭ Sin encuestas aún (grupo futuro o sin actividad)")
                continue

            program_row, module_rows, individual_rows = process_group_data(raw, group_name, course)

            # Saltar si no hay datos nuevos desde la última sincronización.
            # Compara con precisión de minutos (fecha_ultima_dt) para no saltarse
            # respuestas del mismo día registradas después del último sync.
            # Si monday_dt es solo fecha (10 chars), truncamos evol_dt para compatibilidad
            # con ítems que aún no tienen fecha_ultima_dt guardada.
            item_name_prog = f"{course_clean} — {group_name}"
            evol_dt      = program_row.get("fecha_ultima_dt", "") or program_row.get("fecha_ultima", "")
            monday_dt    = program_dates.get(item_name_prog, "")
            cmp_evol     = evol_dt[:len(monday_dt)] if monday_dt else evol_dt
            if cmp_evol and monday_dt and cmp_evol == monday_dt and not dry_run and not force:
                print(f"  ⏭ Sin cambios desde {monday_dt}, omitiendo")
                continue

            # Sección Por Programa
            monday_upsert_item(board_id, group_programa_id, item_name_prog,
                               col_ids, program_row, dry_run, items_cache)

            # Sección Por Módulo — deduplicamos por nombre de ítem
            seen_mod = {}
            for mod_row in module_rows:
                key = f"{course_clean} — {group_name} — {mod_row['modulo']}"
                seen_mod[key] = mod_row  # última entrada gana
            dup_mod = len(module_rows) - len(seen_mod)
            if dup_mod:
                print(f"    ⚡ {dup_mod} módulos duplicados eliminados")
            for item_name_mod, mod_row in seen_mod.items():
                monday_upsert_item(board_id, group_modulo_id, item_name_mod,
                                   col_ids, mod_row, dry_run, items_cache)

            # Sección Por Encuesta — deduplicamos por nombre de ítem
            if group_encuesta_id:
                seen_enc = {}
                for enc_row in individual_rows:
                    alumno = enc_row.get("alumno") or "Anónimo"
                    key = f"{course_clean} — {group_name} — {enc_row['modulo']} — {alumno}"
                    seen_enc[key] = enc_row  # última entrada gana (tiene fechas fallback aplicadas)
                dup_enc = len(individual_rows) - len(seen_enc)
                if dup_enc:
                    print(f"    ⚡ {dup_enc} encuestas duplicadas eliminadas")
                for item_name_enc, enc_row in seen_enc.items():
                    monday_upsert_item(board_id, group_encuesta_id, item_name_enc,
                                       col_ids, enc_row, dry_run, items_cache)

            synced += 1

        except Exception as e:
            print(f"    ✗ Error en grupo {group_name}: {e}")
            if "503" in str(e) or "502" in str(e) or "Service Unavailable" in str(e):
                failed_groups.append(g)
            continue

    # ── Reintentar grupos que fallaron por 503/502 ────────────────────────────
    if failed_groups and not dry_run:
        import time
        print(f"\n  ↺ Reintentando {len(failed_groups)} grupo(s) que fallaron por error de servidor...")
        time.sleep(10)
        retried = 0
        for g in failed_groups:
            group_id   = g["group_id"]
            group_name = g["group_name"]
            course     = g["course"]
            course_clean = course.strip()
            print(f"\n  → (reintento) {course_clean} / {group_name}")
            try:
                raw = get_surveys_by_group(group_id)
                if raw is None:
                    continue
                program_row, module_rows, individual_rows = process_group_data(raw, group_name, course)
                monday_upsert_item(board_id, group_programa_id, f"{course_clean} — {group_name}",
                                   col_ids, program_row, dry_run, items_cache)
                seen_mod = {}
                for mod_row in module_rows:
                    seen_mod[f"{course_clean} — {group_name} — {mod_row['modulo']}"] = mod_row
                for item_name_mod, mod_row in seen_mod.items():
                    monday_upsert_item(board_id, group_modulo_id, item_name_mod,
                                       col_ids, mod_row, dry_run, items_cache)
                if group_encuesta_id:
                    seen_enc = {}
                    for enc_row in individual_rows:
                        alumno = enc_row.get("alumno") or "Anónimo"
                        seen_enc[f"{course_clean} — {group_name} — {enc_row['modulo']} — {alumno}"] = enc_row
                    for item_name_enc, enc_row in seen_enc.items():
                        monday_upsert_item(board_id, group_encuesta_id, item_name_enc,
                                           col_ids, enc_row, dry_run, items_cache)
                synced += 1
                retried += 1
            except Exception as e2:
                print(f"    ✗ Reintento fallido para {group_name}: {e2}")
        if retried:
            print(f"  ✓ {retried} grupo(s) recuperado(s) en el reintento")

    print(f"\n  ✓ Sync completado — {synced} grupos sincronizados {'(simulado)' if dry_run else ''}")
    print(f"     (omitidos: {skipped_course} cursos excluidos, {skipped_group} grupos matrícula abierta)")

    # ── Paso final: regenerar dashboard JSON + inyectar en HTML ───────────────
    # Nota: link_encuestas_alumnos.py se ejecuta como paso separado después del sync
    # En CI (GitHub/Netlify) este paso es redundante: Netlify reconstruye el JSON
    # leyendo ambos boards. Se salta con SKIP_DASHBOARD_REGEN=1 (lo pone el workflow).
    if not dry_run and os.environ.get("SKIP_DASHBOARD_REGEN", "") not in ("1", "true", "yes"):
        step = "2"
        print(f"\n[{step}] Regenerando dashboard (datos_dashboard.json + HTML)...")
        m2d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monday_to_dashboard.py")
        result = subprocess.run(
            [sys.executable, m2d],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if any(k in line for k in ["✓", "⚠️", "❌", "generado", "actualizado", "KB", "nuevos", "[SYNC]"]):
                print(f"  {line.strip()}")
        if result.returncode != 0:
            print(f"  ⚠️  monday_to_dashboard.py terminó con error (código {result.returncode})")


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT a Excel (plantilla de Monday) — para subida manual
# ══════════════════════════════════════════════════════════════════════════════

def _marca_match(m, target):
    """Compara marcas de forma laxa (acentos/espacios/mayúsculas)."""
    import unicodedata
    def n(x):
        x = unicodedata.normalize('NFD', str(x or '').lower())
        x = ''.join(c for c in x if unicodedata.category(c) != 'Mn')
        return re.sub(r'[^a-z0-9]', '', x)
    a, b = n(m), n(target)
    return bool(a) and bool(b) and (a in b or b in a)


def mode_export(excel_path, company=None, filter_course=None, year=None,
                niveles=("encuesta", "modulo", "programa")):
    """Exporta las encuestas a un Excel con la plantilla del board de Monday, SIN
    escribir en Monday. Pensado para subida manual de programas ya finalizados.

    - Recorre TODOS los grupos (también finalizados), no solo los activos.
    - Si company está definido, filtra a los alumnos de esa marca (mapa DNI→Marca
      de Matrículas) y recalcula las medias de módulo/programa solo con ellos.
    - niveles: qué filas incluir (encuesta=por alumno, modulo, programa).
    """
    try:
        import openpyxl
    except ImportError:
        print("✗ Falta 'openpyxl'. Añádelo a requirements.txt.", file=sys.stderr)
        sys.exit(2)

    # 1) DNIs de la empresa (cadena Matrículas → Sociedad → Cuenta)
    allowed = None
    if company:
        try:
            from link_marca_matriculas import build_dni_to_marca
        except Exception as e:
            print(f"✗ No se pudo importar build_dni_to_marca: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"\n[0] DNIs de la empresa '{company}' desde Matrículas...")
        dni_to_marca = build_dni_to_marca() or {}
        allowed = {d for d, m in dni_to_marca.items() if _marca_match(m, company)}
        print(f"   {len(allowed)} DNIs de '{company}' (de {len(dni_to_marca)} con marca).")
        if not allowed:
            print("   ⚠️  0 DNIs para esa empresa. Revisa el mapa DNI→Marca "
                  "(cadena Matrículas→Sociedad→Cuenta, IDs de columna).")

    print("\n[1] Obteniendo grupos de EvolCampus...")
    all_groups = get_all_groups()
    print(f"  {len(all_groups)} grupos en total")

    TEMPLATE_COLS = ['Name', 'Programa', 'Grupo', 'Módulo', 'Formador', 'Alumno', 'DNI',
                     'Alumno (rel)', 'Cuenta empresa', 'Empresa - dashboard', '% Global',
                     'Respuestas', '% Formador', '% Contenido', '% Formato', 'Nº Comentarios',
                     'Fecha última encuesta', 'Comentarios', 'Fecha inicio', 'Fecha fin']
    out_rows = []

    for g in all_groups:
        group_id, group_name, course = g["group_id"], g["group_name"], g["course"]
        if is_course_excluded(course):
            continue
        if filter_course and filter_course.lower() not in course.lower():
            continue
        if is_group_excluded(group_name):
            continue
        if year and not is_group_in_year(group_name, year):
            continue
        course_clean = course.strip()
        try:
            raw = get_surveys_by_group(group_id)
        except Exception as e:
            print(f"  ⚠️  {course_clean} / {group_name}: error EvolCampus ({e}); se omite.")
            continue
        if raw is None:
            continue
        program_row, module_rows, individual_rows = process_group_data(
            raw, group_name, course, allowed_dnis=allowed)
        # Si filtramos por empresa y no hay alumnos de ella en este grupo → saltar
        if allowed is not None and not individual_rows:
            continue
        start_date = program_row.get("fecha_inicio", "")
        end_date   = program_row.get("fecha_fin", "")

        def _add(row, level):
            name = {
                "programa": f"{course_clean} — {group_name}",
                "modulo":   f"{course_clean} — {group_name} — {row.get('modulo','')}",
                "encuesta": f"{course_clean} — {group_name} — {row.get('modulo','')} — {row.get('alumno','')}",
            }[level]
            out_rows.append([
                name, course_clean, group_name, row.get("modulo", ""), row.get("formador", ""),
                row.get("alumno", ""), row.get("dni", ""), "", "", (company or ""),
                row.get("pct_global", ""), row.get("respuestas", ""),
                row.get("pct_formador", ""), row.get("pct_contenido", ""), row.get("pct_formato", ""),
                row.get("n_comentarios", ""), row.get("fecha_ultima", ""), row.get("comentarios", ""),
                start_date, end_date,
            ])

        if "programa" in niveles:
            _add(program_row, "programa")
        if "modulo" in niveles:
            for mr in module_rows:
                _add(mr, "modulo")
        if "encuesta" in niveles:
            for ir in individual_rows:
                _add(ir, "encuesta")
        print(f"  ✓ {course_clean} / {group_name}: {len(individual_rows)} encuestas · {len(module_rows)} módulos")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "encuestas programas"
    ws.append(TEMPLATE_COLS)
    for r in out_rows:
        ws.append(r)
    wb.save(excel_path)
    print(f"\n✅ Export: {len(out_rows)} filas → {excel_path}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sincronización EvolCampus → Monday.com (Cursos)")
    parser.add_argument("--explore",  action="store_true", help="Explorar estructura de datos (solo lectura)")
    parser.add_argument("--setup",    action="store_true", help="Crear board en Monday (solo 1ª vez)")
    parser.add_argument("--sync",     action="store_true", help="Sincronizar datos")
    parser.add_argument("--dry-run",  action="store_true", help="Simular sync sin escribir en Monday")
    parser.add_argument("--year",          type=int, metavar="YYYY",
                        help="Sincronizar todos los grupos del año indicado (incluye pasados). Ej: --year 2025")
    parser.add_argument("--filter-course", metavar="TEXTO",
                        help="Sincronizar solo grupos cuyo curso contenga este texto. Ej: --filter-course 'LGTBI'")
    parser.add_argument("--force", action="store_true",
                        help="Ignora el salto 'sin cambios' por fecha y reprocesa todos los grupos (backfill de respuestas tardías).")
    parser.add_argument("--export-excel", metavar="PATH",
                        help="Exporta encuestas a un Excel con la plantilla de Monday (NO escribe en Monday). Para subida manual.")
    parser.add_argument("--company", metavar="TEXTO",
                        help="Filtra el export a los alumnos de esa marca/empresa. Ej: --company 'Garden Hotels'")
    parser.add_argument("--niveles", metavar="LISTA", default="encuesta,modulo,programa",
                        help="Niveles a exportar (coma): encuesta,modulo,programa. Por defecto los tres.")
    args = parser.parse_args()

    if not any([args.explore, args.setup, args.sync, args.export_excel]):
        parser.print_help()
        sys.exit(0)

    if args.explore:
        mode_explore()
    if args.setup:
        monday_setup_board()
    if args.sync:
        mode_sync(dry_run=args.dry_run, year=args.year, filter_course=args.filter_course, force=args.force)
    if args.export_excel:
        mode_export(args.export_excel, company=args.company, filter_course=args.filter_course,
                    year=args.year,
                    niveles=tuple(s.strip() for s in (args.niveles or "").split(",") if s.strip()))
