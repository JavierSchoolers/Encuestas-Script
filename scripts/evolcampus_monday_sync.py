"""
evolcampus_monday_sync.py
─────────────────────────────────────────────────────────────────────────────
Integración automática: EvolCampus API → Monday.com + Dashboard JSON

Modos de uso:
  python3 evolcampus_monday_sync.py --explore      # Ver estructura de datos (solo lectura)
  python3 evolcampus_monday_sync.py --setup        # Crear el board en Monday (solo 1ª vez)
  python3 evolcampus_monday_sync.py --sync         # Sincronizar todos los datos
  python3 evolcampus_monday_sync.py --sync --dry-run  # Simular sin escribir en Monday

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
MONDAY_BOARD_NAME    = "Encuestas: Prueba"

# Palabras clave para detectar categoría de pregunta
FORMADOR_KW  = ["formador", "ponente", "docente", "profesor", "tutor", "trainer"]
CONTENIDO_KW = ["contenido", "materiales", "temario", "información", "aprendizaje", "conocimiento"]
FORMATO_KW   = ["formato", "plataforma", "herramienta", "metodología", "dinámica", "organización", "estructura"]

# ID del board de Monday (se rellena automáticamente tras --setup, o puedes ponerlo manualmente)
MONDAY_BOARD_ID = None   # Ejemplo: 1234567890

# ── Encuestas excluidas (por nombre de survey, parcial, case-insensitive) ────
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
    """Obtiene y cachea el JWT de EvolCampus."""
    global _evol_token
    if _evol_token:
        return _evol_token
    resp = requests.post(f"{EVOLCAMPUS_API}/token",
                         json={"clientid": EVOLCAMPUS_CLIENT_ID, "key": EVOLCAMPUS_KEY},
                         timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # El token puede venir como {"token": "..."} o {"access_token": "..."}
    _evol_token = data.get("token") or data.get("access_token") or data.get("Token")
    if not _evol_token:
        raise ValueError(f"No se encontró token en la respuesta: {data}")
    print(f"  ✓ Token EvolCampus obtenido")
    return _evol_token


def evol_get(endpoint):
    """GET autenticado a la API de EvolCampus."""
    headers = {"Authorization": f"Bearer {evol_token()}"}
    resp = requests.get(f"{EVOLCAMPUS_API}/{endpoint}", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def evol_post(endpoint, payload):
    """POST autenticado a la API de EvolCampus."""
    headers = {"Authorization": f"Bearer {evol_token()}"}
    resp = requests.post(f"{EVOLCAMPUS_API}/{endpoint}", headers=headers,
                         json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_all_groups():
    """Devuelve lista de {course, group_id, group_name}."""
    data = evol_get("getCoursesGroups")

    # Guardar respuesta cruda para diagnóstico
    with open("raw_getCoursesGroups.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    groups = []
    items = data if isinstance(data, list) else data.get("courses", data.get("groups", data.get("data", [])))

    for item in items:
        # Estructura real: {"id": 597, "name": "Curso X", "groups": [{"groupid": "2511", "group": "Nombre grupo"}]}
        course_name = item.get("name", "")
        for group in item.get("groups", []):
            groups.append({
                "course":     course_name,
                "group_id":   group.get("groupid"),
                "group_name": group.get("group", ""),
            })
    return groups


def get_surveys_by_group(group_id, use_activities=False):
    """Devuelve los datos de encuestas de un grupo.
    use_activities=True solo para EGH/CSUL (que tienen subnivel de actividades).
    """
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        gid = group_id
    headers = {"Authorization": f"Bearer {evol_token()}"}
    payload = {"groupid": gid}
    if use_activities:
        payload["activities"] = True
    resp = requests.post(f"{EVOLCAMPUS_API}/getSurveysByGroup",
                         headers=headers, json=payload, timeout=30)
    if resp.status_code == 400:
        # La API devuelve 400 cuando el grupo aún no tiene encuestas (curso futuro o sin actividad)
        return None
    if not resp.ok:
        print(f"    Detalle error API: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# PROCESAMIENTO DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

def strip_html(text):
    """Elimina etiquetas HTML de un texto."""
    return re.sub(r'<[^>]+>', ' ', text or "").strip()


def categorize_question(question_text):
    """Devuelve 'formador', 'contenido', 'formato' o 'otro' según el texto (sin HTML)."""
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
    """Clave para ordenación natural: 1.2 < 1.10 < 2.1"""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]


def extract_subject_number(module_name):
    """Extrae el número de asignatura del nombre del módulo.
    Ej: '2.1. La sostenibilidad' → '2'
        '1.10. Liderazgo enfocado' → '1'
    """
    m = re.match(r'^(\d+)[\.\-]', module_name.strip())
    if m:
        return m.group(1)
    m = re.match(r'(?:módulo|bloque|unidad|tema)\s+(\d+)', module_name.strip(), re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def build_subjects_map(courses_data, course_name):
    """
    Construye un mapa {número: nombre_asignatura} para un curso dado.
    Ej: {'1': '1. Liderazgo, transformación y gestión responsable (ESG)', '2': '2. Gestión operativa...'}
    """
    subjects_map = {}
    courses = courses_data if isinstance(courses_data, list) else courses_data.get("courses", [])
    for course in courses:
        if course.get("name", "").strip() == course_name.strip():
            for s in course.get("subjects", []):
                subj_name = s.get("subject", "")
                num = extract_subject_number(subj_name)
                if num:
                    subjects_map[num] = subj_name
            break
    return subjects_map


def extract_formador_name(question_html):
    """Extrae el nombre del formador del texto de la pregunta HTML.

    Maneja dos formatos:
      Caso A: <strong>formadora Laura Garrido?</strong>
              (formador/a y nombre dentro del mismo tag)
      Caso B: nuestro formador <strong>Rubén Marín?</strong>
              (formador/a fuera del tag, nombre dentro del siguiente tag)
    """
    # Caso A: "formador[a] NombreApellido" en el mismo nodo de texto
    match = re.search(r'formador[a]?\s+([^<?\n]{3,40})', question_html, re.IGNORECASE)
    if match:
        name = match.group(1).strip().rstrip('?').strip()
        if len(name) >= 3:
            return name
    # Caso B: "formador[a] <tag>NombreApellido</tag>"
    match = re.search(r'formador[a]?\s*<[^>]+>\s*([^<?\n]{3,40})', question_html, re.IGNORECASE)
    if match:
        name = match.group(1).strip().rstrip('?').strip()
        if len(name) >= 3:
            return name
    return None


def to_pct(scores):
    """Convierte lista de puntuaciones (escala 1-4) a porcentaje 0-100."""
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return round((avg / 4) * 100, 1)


def is_real_comment(text):
    """Devuelve True si el texto parece un comentario real (no solo 'No', 'nada', etc.)"""
    if not text or not str(text).strip():
        return False
    t = str(text).strip().lower()
    # Respuestas negativas cortas no cuentan como comentario
    negative = {"no", "nada", "ninguno", "ninguna", "no de momento", "de momento no",
                "ningún comentario", "ningun comentario", "no, gracias", "no gracias",
                "sin comentarios", "sin sugerencias", "-", ".", "n/a"}
    return t not in negative and len(t) > 3


def egh_parte_from_module(module_name, course_name):
    """Clasifica módulos EGH en Parte 1 / Parte 2 / Parte 3.
    Parte 1: asignaturas 1.x–4.x (Liderazgo, Sostenibilidad, Transformación digital, Modelos)
    Parte 2: Gestión operativa (Front desk, F&B, Housekeeping, Technical Services…)
    Parte 3: Dirección estratégica (Estrategia, Ofimática, Finanzas, Revenue, Idiomas, Diversidad…)
    """
    if not ("EGH" in (course_name or "") or "Hospitality" in (course_name or "")):
        return None
    m_str = str(module_name).strip()
    if re.match(r'^[1-4][\.\-]', m_str):
        return "Parte 1"
    mod_low = m_str.lower()
    p2 = ["front desk","f&b","food","beverage","housekeeping","technical service",
          "servicio técnico","guest experience","recepción","pisos","alimentos","bebidas"]
    if any(k in mod_low for k in p2):
        return "Parte 2"
    p3 = ["estrategia","ofimática","ofimatica","finanzas","revenue","comercial",
          "marketing","alemán","aleman","inglés","ingles","diversidad"]
    if any(k in mod_low for k in p3):
        return "Parte 3"
    return None


def parse_group_dates(group_name):
    """
    Extrae (start_date, end_date) en formato YYYY-MM-DD del nombre del grupo.
    Soporta formatos como:
      '16/02 al 15/12 - 2026'
      '16/02/2026 al 15/02/2027 - 2026'
      '23/03 al 15/12 - 2026'
    Devuelve ("", "") si no puede parsear.
    """
    gn = group_name or ""

    # Formato largo con "al": DD/MM/YYYY al DD/MM/YYYY
    m = re.search(r'(\d{1,2}/\d{1,2}/(\d{4}))\s+al\s+(\d{1,2}/\d{1,2}/(\d{4}))', gn)
    if m:
        try:
            start = datetime.strptime(m.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
            end   = datetime.strptime(m.group(3), "%d/%m/%Y").strftime("%Y-%m-%d")
            return start, end
        except Exception:
            pass

    # Formato corto con "al": DD/MM al DD/MM - YYYY (año al final)
    m = re.search(r'(\d{1,2}/\d{1,2})\s+al\s+(\d{1,2}/\d{1,2})\s*[-–]\s*(\d{4})', gn)
    if m:
        try:
            year  = m.group(3)
            start = datetime.strptime(f"{m.group(1)}/{year}", "%d/%m/%Y").strftime("%Y-%m-%d")
            end   = datetime.strptime(f"{m.group(2)}/{year}", "%d/%m/%Y").strftime("%Y-%m-%d")
            return start, end
        except Exception:
            pass

    # Formato largo con " - ": DD/MM/YYYY - DD/MM/YYYY  (ej: CSUL - ... 23/03/2026 - 15/12/2026)
    m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})\s*[-–]\s*(\d{1,2}/\d{1,2}/\d{4})', gn)
    if m:
        try:
            start = datetime.strptime(m.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
            end   = datetime.strptime(m.group(2), "%d/%m/%Y").strftime("%Y-%m-%d")
            return start, end
        except Exception:
            pass

    return "", ""


def process_group_data(raw_data, group_name, course_name, subjects_map=None):
    """
    Procesa la respuesta cruda de getSurveysByGroup.
    Estructura real: raw_data = {"group": {"name": ..., "course": ..., "surveys": [...]}}
    Devuelve:
      - program_row: dict con métricas agregadas del programa completo
      - module_rows: lista de dicts, uno por módulo
    """
    # Determinar si es EGH/CSUL (usan nombre de actividad como módulo)
    # Para el resto, el módulo viene del campo "asignatura" o "subject" de cada survey
    _is_egh_csul = any(k in (course_name or "") for k in ["EGH", "Executive Global Hospitality", "CSUL"])

    # Extraer surveys — estructura real: data["group"]["surveys"]
    grp = raw_data.get("group", raw_data)
    surveys = grp.get("surveys", [])

    # Usar nombre de curso/grupo del API si está disponible
    course_name = grp.get("course") or course_name
    group_name  = grp.get("name") or group_name

    program_scores_global    = []
    program_scores_formador  = []
    program_scores_contenido = []
    program_scores_formato   = []
    program_n_responses      = set()
    program_n_comments       = 0
    program_last_date        = None

    module_rows    = []
    individual_rows = []

    for survey in surveys:
        # Ignorar encuestas FUNDAE (administrativas)
        if survey.get("type", "").lower() == "fundae":
            continue

        # Excluir encuestas de programa global (se tratan aparte)
        _sname = (survey.get("name") or "").strip().lower()
        if any(kw in _sname for kw in EXCLUDED_SURVEY_NAMES):
            continue

        if _is_egh_csul:
            # EGH/CSUL: el nombre de la actividad ES el nombre del módulo
            module_name = survey.get("name") or "Sin nombre"
            # Limpiar sufijos como ": encuesta", ": encuestas"
            module_name = re.sub(r':\s*encuest[as]+\s*$', '', module_name, flags=re.IGNORECASE).strip()
        else:
            # Resto de programas: el nombre del módulo (Asignatura) está dentro de cada record
            _first_rec = (survey.get("records") or [{}])[0]
            module_name = (_first_rec.get("asignatura") or _first_rec.get("subject") or
                           _first_rec.get("module") or survey.get("name") or "Sin nombre")
            module_name = re.sub(r':\s*encuest[as]+\s*$', '', module_name, flags=re.IGNORECASE).strip()

        records = survey.get("records", [])

        # Intentar fecha a nivel de survey (fallback para todos sus records)
        survey_date = None
        for date_key in ("date", "fecha", "fecha_encuesta", "submission_date", "answered_at", "created_at", "end_date"):
            sdate = survey.get(date_key)
            if sdate:
                try:
                    survey_date = datetime.fromisoformat(str(sdate)[:10])
                except Exception:
                    pass
                if survey_date:
                    break

        mod_scores_global    = []
        mod_scores_formador  = []
        mod_scores_contenido = []
        mod_scores_formato   = []
        mod_formador_scores  = {}   # {nombre_formador: [puntuaciones]} para cálculo individual
        mod_n_responses      = 0
        mod_n_comments       = 0
        mod_comments         = []   # lista de {"text": ..., "student": ...}
        mod_last_date        = None
        mod_individual_rows  = []   # filas individuales por alumno

        for record in records:
            student   = record.get("name") or "Anónimo"
            student_dni = record.get("id_card") or ""
            questions = record.get("questions", [])
            has_answer = False

            rec_scores_global    = []
            rec_scores_formador  = []
            rec_scores_contenido = []
            rec_scores_formato   = []
            rec_comments         = []
            rec_last_date        = None

            # Fecha a nivel de registro (antes de iterar preguntas)
            for date_key in ("date", "fecha", "fecha_cuestionario", "submission_date", "answered_at", "created_at"):
                rdate = record.get(date_key)
                if rdate:
                    try:
                        d = datetime.fromisoformat(str(rdate)[:10])
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

                # Fecha a nivel de respuesta (como fallback)
                if rec_last_date is None:
                    for date_key in ("date", "answered_at", "created_at"):
                        qdate = answer.get(date_key)
                        if qdate:
                            try:
                                d = datetime.fromisoformat(str(qdate)[:10])
                                rec_last_date = d
                                if mod_last_date is None or d > mod_last_date:
                                    mod_last_date = d
                                if program_last_date is None or d > program_last_date:
                                    program_last_date = d
                            except Exception:
                                pass
                            break

                # Respuestas numéricas (type 0)
                if qtype == 0:
                    val = answer.get("value_answer")
                    if val is not None:
                        try:
                            val = float(val)
                            cat = categorize_question(qtext)
                            rec_scores_global.append(val)
                            mod_scores_global.append(val)
                            program_scores_global.append(val)
                            if cat == "formador":
                                rec_scores_formador.append(val)
                                mod_scores_formador.append(val)
                                program_scores_formador.append(val)
                                # Tracking por formador individual
                                fname = extract_formador_name(qtext)
                                if fname:
                                    if fname not in mod_formador_scores:
                                        mod_formador_scores[fname] = []
                                    mod_formador_scores[fname].append(val)
                            elif cat == "contenido":
                                rec_scores_contenido.append(val)
                                mod_scores_contenido.append(val)
                                program_scores_contenido.append(val)
                            elif cat == "formato":
                                rec_scores_formato.append(val)
                                mod_scores_formato.append(val)
                                program_scores_formato.append(val)
                            has_answer = True
                        except (ValueError, TypeError):
                            pass

                # Comentarios (type 1 = texto libre) — guardamos todos sin filtrar
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
                # Construir fila individual
                mod_individual_rows.append({
                    "curso":          course_name,
                    "grupo":          group_name,
                    "modulo":         module_name,
                    "alumno":         student,
                    "dni":            student_dni,
                    "pct_global":     to_pct(rec_scores_global),
                    "pct_formador":   to_pct(rec_scores_formador),
                    "pct_contenido":  to_pct(rec_scores_contenido),
                    "pct_formato":    to_pct(rec_scores_formato),
                    "n_comentarios":  len(rec_comments),
                    "comentarios":    json.dumps([{"text": c, "student": student, "date": rec_last_date.strftime("%Y-%m-%d") if rec_last_date else ""} for c in rec_comments], ensure_ascii=False) if rec_comments else "",
                    "fecha_ultima":   rec_last_date.strftime("%Y-%m-%d") if rec_last_date else "",
                    "fecha_encuesta": rec_last_date.strftime("%Y-%m-%d") if rec_last_date else "",
                })

        # Fallback de fecha: si un record no tiene fecha, usar mod_last_date o survey_date
        _fecha_fallback = (
            mod_last_date or survey_date
        )
        if _fecha_fallback:
            for ir in mod_individual_rows:
                if not ir.get("fecha_encuesta"):
                    ir["fecha_encuesta"] = _fecha_fallback.strftime("%Y-%m-%d")
                if not ir.get("fecha_ultima"):
                    ir["fecha_ultima"] = _fecha_fallback.strftime("%Y-%m-%d")
            if mod_last_date is None:
                mod_last_date = _fecha_fallback

        subj_num   = extract_subject_number(module_name)
        asignatura = (subjects_map or {}).get(subj_num, subj_num)

        # Añadir asignatura a las filas individuales de este módulo
        for ir in mod_individual_rows:
            ir["asignatura"] = asignatura
        individual_rows.extend(mod_individual_rows)

        # Calcular pct_formador como media de las medias individuales (cada formador pesa igual)
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
            "asignatura":     asignatura,
            "modulo":         module_name,
            "formador":       " / ".join(formadores_ordenados),
            "pct_global":     to_pct(mod_scores_global),
            "respuestas":     mod_n_responses,
            "pct_formador":   pct_formador_overall,
            "pct_contenido":  to_pct(mod_scores_contenido),
            "pct_formato":    to_pct(mod_scores_formato),
            "n_comentarios":  mod_n_comments,
            "comentarios":    json.dumps(mod_comments, ensure_ascii=False) if mod_comments else "",
            "fecha_ultima":   mod_last_date.strftime("%Y-%m-%d") if mod_last_date else "",
        }
        # Añadir formadores individuales (hasta 4)
        for i, fname in enumerate(formadores_ordenados[:6], 1):
            mod_row[f"formador_{i}"]     = fname
            mod_row[f"pct_formador_{i}"] = to_pct(mod_formador_scores[fname])

        module_rows.append(mod_row)

    # Ordenar módulos: primero por asignatura, luego por nombre de módulo (orden natural)
    module_rows.sort(key=lambda x: (natural_key(x["asignatura"]), natural_key(x["modulo"])))

    # Fechas del grupo (desde el nombre)
    start_date, end_date = parse_group_dates(group_name)

    program_row = {
        "curso":          course_name,
        "grupo":          group_name,
        "asignatura":     "",
        "modulo":         "(Programa completo)",
        "formador":       "",
        "pct_global":     to_pct(program_scores_global),
        "respuestas":     len(program_n_responses),
        "pct_formador":   to_pct(program_scores_formador),
        "pct_contenido":  to_pct(program_scores_contenido),
        "pct_formato":    to_pct(program_scores_formato),
        "n_comentarios":  program_n_comments,
        "fecha_ultima":   program_last_date.strftime("%Y-%m-%d") if program_last_date else "",
        "fecha_inicio":   start_date,
        "fecha_fin":      end_date,
    }

    return program_row, module_rows, individual_rows


# ══════════════════════════════════════════════════════════════════════════════
# MONDAY.COM API
# ══════════════════════════════════════════════════════════════════════════════

def monday_query(query, variables=None, retries=4):
    """Ejecuta una query/mutation GraphQL en Monday.com (con reintentos)."""
    headers = {
        "Authorization": MONDAY_TOKEN,
        "Content-Type":  "application/json",
        "API-Version":   "2024-01",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    # Reintentos ante timeouts / errores transitorios de red o servidor (429/5xx).
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


def monday_list_boards():
    """Lista los boards del workspace."""
    q = """
    query {
      boards(limit: 50) {
        id
        name
        state
      }
    }
    """
    data = monday_query(q)
    return data.get("boards", [])


def monday_setup_board():
    """
    Crea el board 'Encuestas: Prueba' con las dos secciones y todas las columnas.
    Devuelve el board_id.
    """
    # 1. Crear board
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

    # 2. Crear columnas
    columns = [
        ("grupo",          "Grupo",                  "text"),
        ("asignatura",     "Asignatura",             "text"),
        ("modulo",         "Módulo",                 "text"),
        ("formador",       "Formador",               "text"),
        ("pct_global",     "% Global",               "numbers"),
        ("respuestas",     "Respuestas",             "numbers"),
        ("pct_formador",   "% Formador",             "numbers"),
        ("pct_contenido",  "% Contenido",            "numbers"),
        ("pct_formato",    "% Formato",              "numbers"),
        ("n_comentarios",  "Nº Comentarios",         "numbers"),
        ("fecha_ultima",   "Fecha última encuesta",  "date"),
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

    # 3. Crear grupos (secciones)
    q_group = """
    mutation($board_id: ID!, $name: String!) {
      create_group(board_id: $board_id, group_name: $name) {
        id
      }
    }
    """
    d1 = monday_query(q_group, {"board_id": board_id, "name": "Por Programa"})
    d2 = monday_query(q_group, {"board_id": board_id, "name": "Por Módulo"})
    group_programa_id = d1["create_group"]["id"]
    group_modulo_id   = d2["create_group"]["id"]
    print(f"  ✓ Secciones creadas: Por Programa ({group_programa_id}), Por Módulo ({group_modulo_id})")

    # Guardar IDs en un archivo de configuración local
    config = {
        "board_id":          board_id,
        "group_programa_id": group_programa_id,
        "group_modulo_id":   group_modulo_id,
        "col_ids":           col_ids,
    }
    with open("monday_config.json", "w") as f:
        json.dump(config, f, indent=2)
    print(f"  ✓ Configuración guardada en monday_config.json")

    return config


def load_monday_config():
    """Carga la configuración del board desde monday_config.json."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monday_config.json")
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            "No se encontró monday_config.json. Ejecuta primero: python3 evolcampus_monday_sync.py --setup"
        )


def preload_board_items(board_id):
    """Pre-carga todos los items del board en un dict {name: item_id}.
    Evita hacer una búsqueda API por nombre en cada upsert (mucho más rápido)."""
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


def preload_program_dates(board_id, group_programa_id, fecha_col_id):
    """Pre-carga la fecha de última encuesta de cada ítem 'Por Programa'.
    Devuelve {item_name: date_str} para detectar grupos sin cambios y saltarlos."""
    if not fecha_col_id:
        return {}
    query = """
    query($boardId: [ID!]!, $groupId: [String!]!, $cols: [String!]!) {
        boards(ids: $boardId) {
            groups(ids: $groupId) {
                items_page(limit: 500) {
                    items {
                        name
                        column_values(ids: $cols) { text }
                    }
                }
            }
        }
    }
    """
    data = monday_query(query, {
        "boardId": [str(board_id)],
        "groupId": [group_programa_id],
        "cols": [fecha_col_id],
    })
    items = (data.get("boards", [{}])[0]
                 .get("groups", [{}])[0]
                 .get("items_page", {})
                 .get("items", []))
    dates_map = {}
    for item in items:
        cv = item.get("column_values", [])
        dates_map[item["name"]] = cv[0]["text"] if cv else ""
    print(f"  ✓ {len(dates_map)} fechas pre-cargadas de 'Por Programa'\n")
    return dates_map


def monday_upsert_item(board_id, group_id, item_name, col_ids, row, dry_run=False, items_cache=None):
    """
    Crea o actualiza un ítem en Monday.
    Si se pasa items_cache (dict {name: id}), evita la búsqueda individual por nombre.
    """
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
    # Formadores individuales (hasta 4)
    for i in range(1, 7):
        add(f"formador_{i}",     row.get(f"formador_{i}", ""))
        add(f"pct_formador_{i}", row.get(f"pct_formador_{i}"))

    # Comentarios: long-text requiere un dict con clave "text"
    comentarios_raw = row.get("comentarios", "")
    if comentarios_raw and "comentarios" in col_ids:
        col_values[col_ids["comentarios"]] = {"text": comentarios_raw}

    # Columnas de fecha — requieren formato especial en Monday (objeto, no string)
    fecha = row.get("fecha_ultima", "")
    if fecha and "fecha_ultima" in col_ids:
        col_values[col_ids["fecha_ultima"]] = {"date": fecha}

    for fecha_key in ("fecha_inicio", "fecha_fin", "fecha_encuesta"):
        fval = row.get(fecha_key, "")
        if fval and fecha_key in col_ids:
            col_values[col_ids[fecha_key]] = {"date": fval}

    col_values_str = json.dumps(col_values)

    if dry_run:
        print(f"    [DRY-RUN] Ítem: {item_name} | {col_values}")
        return

    # Buscar si ya existe — prueba el nombre actual y el formato legacy (con doble espacio
    # antes de " — ", que se generaba cuando course tenía trailing space antes del strip())
    q_search = """
    query($board_id: ID!, $name: String!) {
      items_page_by_column_values(board_id: $board_id, limit: 5,
        columns: [{column_id: "name", column_values: [$name]}]) {
        items { id name }
      }
    }
    """
    def _search_items(name):
        try:
            result = monday_query(q_search, {"board_id": board_id, "name": name})
            return result.get("items_page_by_column_values", {}).get("items", [])
        except Exception:
            return []

    # Buscar en caché local primero (si está disponible), si no, búsqueda individual en Monday
    if items_cache is not None:
        item_id = items_cache.get(item_name)
        if item_id is None:
            item_name_legacy = item_name.replace(" — ", "  — ", 1)
            item_id = items_cache.get(item_name_legacy)
    else:
        items = _search_items(item_name)
        if not items:
            item_name_legacy = item_name.replace(" — ", "  — ", 1)
            items = _search_items(item_name_legacy)
        item_id = items[0]["id"] if items else None

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
        monday_query(q_create, {
            "board_id":   board_id,
            "group_id":   group_id,
            "name":       item_name,
            "col_values": col_values_str,
        })
        print(f"    + Creado: {item_name}")


# ══════════════════════════════════════════════════════════════════════════════
# MODOS DE EJECUCIÓN
# ══════════════════════════════════════════════════════════════════════════════

def mode_explore():
    """Muestra la estructura de datos de EvolCampus sin escribir nada."""
    print("\n── EXPLORACIÓN EVOLCAMPUS ──────────────────────────────")

    print("\n[1] Listando cursos y grupos...")
    groups = get_all_groups()
    print(f"  Total grupos encontrados: {len(groups)}")
    for g in groups[:20]:
        print(f"  · [{g['group_id']}] {g['course']} — {g['group_name']}")
    if len(groups) > 20:
        print(f"  ... y {len(groups)-20} más")
    print(f"\n  ✓ Respuesta cruda de getCoursesGroups guardada en raw_getCoursesGroups.json")

    if not groups:
        print("  ✗ No se encontraron grupos")
        return

    # Probar grupos hasta encontrar uno con datos
    # Prioridad: PAHK Promo 54 primero
    PRIORITY_IDS = ["8803", "8806", "7008"]
    survey_raw = None
    groups_to_try = [g for g in groups if str(g["group_id"]) in PRIORITY_IDS]
    groups_to_try += [g for g in groups if str(g["group_id"]) not in PRIORITY_IDS][:3]

    for tg in groups_to_try:
        print(f"\n[2] Probando encuestas del grupo: {tg['group_name']} (ID: {tg['group_id']})...")
        try:
            # Llamada directa sin filtro de 400 para ver la estructura cruda
            headers = {"Authorization": f"Bearer {evol_token()}"}
            resp_raw = requests.post(
                f"{EVOLCAMPUS_API}/getSurveysByGroup",
                headers=headers,
                json={"groupid": tg["group_id"], "activities": True},
                timeout=30
            )
            print(f"  HTTP {resp_raw.status_code}")
            raw_json = resp_raw.json()
            with open("explore_output.json", "w", encoding="utf-8") as f:
                json.dump(raw_json, f, indent=2, ensure_ascii=False)
            print(f"  ✓ Respuesta completa guardada en explore_output.json")
            if resp_raw.status_code == 200:
                survey_raw = raw_json
                test_group = tg
                break
            else:
                print(f"  Respuesta: {json.dumps(raw_json, ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"  · Error: {e}")
            continue

    if survey_raw:
        with open("explore_output.json", "w", encoding="utf-8") as f:
            json.dump(survey_raw, f, indent=2, ensure_ascii=False)
        print(f"\n  Estructura de respuesta (primeros 2000 caracteres):")
        print(json.dumps(survey_raw, indent=2, ensure_ascii=False)[:2000])
        print(f"\n  ✓ Respuesta completa guardada en explore_output.json")

        # Mostrar claves del primer record para detectar el campo de fecha
        grp = survey_raw.get("group", survey_raw)
        surveys = grp.get("surveys", [])
        for sv in surveys:
            records = sv.get("records", [])
            if records:
                first_rec = records[0]
                print(f"\n  ── Claves del primer record (survey '{sv.get('name','')}'):")
                for k, v in first_rec.items():
                    if k != "questions":
                        print(f"    {k!r}: {str(v)[:80]}")
                questions = first_rec.get("questions", [])
                if questions:
                    first_q = questions[0]
                    answer = first_q.get("learner_answer") or {}
                    print(f"\n  ── Claves del primer answer:")
                    for k, v in answer.items():
                        print(f"    {k!r}: {str(v)[:80]}")
                break
    else:
        print(f"\n  ✗ Ningún grupo de prueba tiene datos de encuestas todavía.")
        print(f"  Esto es normal si los alumnos aún no han completado encuestas.")
        print(f"  Puedes ejecutar --setup cuando quieras crear el board en Monday.")

    print("\n[3] Listando boards de Monday...")
    try:
        boards = monday_list_boards()
        for b in boards:
            print(f"  · [{b['id']}] {b['name']} ({b['state']})")
    except Exception as e:
        print(f"  ✗ Error Monday: {e}")


def mode_test_local(filepath):
    """Prueba el procesamiento sobre un archivo JSON local sin llamar a ninguna API."""
    print(f"\n── TEST LOCAL: {filepath} ──────────────────────────────")
    with open(filepath, encoding="utf-8") as f:
        raw = json.load(f)
    grp = raw.get("group", raw)
    course_name = grp.get("course", "")

    # Cargar mapa de asignaturas desde raw_getCoursesGroups.json si existe
    subjects_map = {}
    try:
        with open("raw_getCoursesGroups.json", encoding="utf-8") as f:
            courses_data = json.load(f)
        subjects_map = build_subjects_map(courses_data, course_name)
        print(f"  Asignaturas encontradas: {subjects_map}")
    except FileNotFoundError:
        print(f"  ⚠ raw_getCoursesGroups.json no encontrado, asignaturas sin nombre completo")

    program_row, module_rows, individual_rows = process_group_data(raw, grp.get("name",""), course_name, subjects_map)
    print(f"\n  PROGRAMA COMPLETO:")
    for k, v in program_row.items():
        print(f"    {k}: {v}")
    print(f"\n  MÓDULOS ({len(module_rows)}):")
    for m in module_rows:
        print(f"    [Asig.{m['asignatura']}] {m['modulo']}")
        print(f"      Formador: {m['formador']}")
        print(f"      %Global={m['pct_global']} | Resp={m['respuestas']} | %Form={m['pct_formador']} | %Cont={m['pct_contenido']} | %Fmt={m['pct_formato']} | Coment={m['n_comentarios']}")
    print(f"\n  ENCUESTAS INDIVIDUALES ({len(individual_rows)}):")
    for r in individual_rows[:10]:
        print(f"    {r['alumno']} | {r['modulo']} | %Global={r['pct_global']} | Coment={r['n_comentarios']}")
    if len(individual_rows) > 10:
        print(f"    ... y {len(individual_rows)-10} más")


def mode_setup():
    """Crea el board en Monday (ejecutar solo una vez)."""
    print("\n── SETUP MONDAY BOARD ──────────────────────────────────")
    print(f"  Creando board: '{MONDAY_BOARD_NAME}'...")
    config = monday_setup_board()
    print(f"\n  ✓ Setup completado. Board ID: {config['board_id']}")
    print("  Ahora puedes ejecutar: python3 evolcampus_monday_sync.py --sync")


def mode_sync(dry_run=False, filter_course=None, force=False):
    """Sincroniza todos los grupos activos con Monday.
    force=True ignora el salto 'sin cambios' por fecha y reprocesa todos los grupos."""
    print(f"\n── SYNC {'[DRY-RUN] ' if dry_run else ''}──────────────────────────────────────")

    config = load_monday_config()
    board_id           = config["board_id"]
    group_programa_id  = config["group_programa_id"]
    group_modulo_id    = config["group_modulo_id"]
    group_encuesta_id  = config.get("group_encuesta_id", "")
    col_ids            = config["col_ids"]

    # Pre-cargar items del board para evitar búsquedas individuales
    if not dry_run:
        items_cache = preload_board_items(board_id)
        fecha_col_id = col_ids.get("fecha_ultima", "")
        program_dates = preload_program_dates(board_id, group_programa_id, fecha_col_id)
    else:
        items_cache = {}
        program_dates = {}

    failed_groups = []

    print("\n[1] Obteniendo grupos de EvolCampus...")
    all_groups = get_all_groups()
    print(f"  {len(all_groups)} grupos encontrados")

    # Cargar datos de cursos para mapa de asignaturas
    try:
        with open("raw_getCoursesGroups.json", encoding="utf-8") as f:
            courses_data = json.load(f)
    except FileNotFoundError:
        courses_data = {}

    for g in all_groups:
        group_id   = g["group_id"]
        group_name = g["group_name"]
        course     = g["course"]

        if filter_course and filter_course.lower() not in course.lower():
            continue

        # Omitir cursos demo/administrativos
        _skip = ["EISI HOTEL by Schôolers", "EISI HOTEL by Schoolers",
                 "¡Bienvenidos a Schôolers", "Bienvenidos a Schôolers",
                 "Examples"]
        if any(s.lower() in course.lower() for s in _skip):
            print(f"  ⏭ Omitido (demo/admin): {course}")
            continue

        # Solo sincronizar cursos EGH / Executive Global Hospitality
        _egh_keywords = ["EGH", "Executive Global Hospitality", "Gestión Responsable"]
        if not any(k.lower() in course.lower() for k in _egh_keywords):
            print(f"  ⏭ Omitido (no EGH): {course}")
            continue

        course_clean = course.strip()   # eliminar espacios extra del nombre del curso
        print(f"\n  → {course_clean} / {group_name}")
        try:
            raw          = get_surveys_by_group(group_id, use_activities=True)
            if raw is None:
                print(f"  ⏭ Sin encuestas aún (grupo futuro o sin actividad)")
                continue
            subjects_map = build_subjects_map(courses_data, course)
            program_row, module_rows, individual_rows = process_group_data(raw, group_name, course, subjects_map)

            # Saltar si no hay datos nuevos desde la última sincronización
            item_name_prog = f"{course_clean} — {group_name}"
            evol_fecha  = program_row.get("fecha_ultima", "")
            monday_fecha = program_dates.get(item_name_prog, "")
            if evol_fecha and monday_fecha and evol_fecha == monday_fecha and not dry_run and not force:
                print(f"  ⏭ Sin cambios desde {evol_fecha}, omitiendo")
                continue

            # Sección Por Programa
            monday_upsert_item(board_id, group_programa_id, item_name_prog,
                               col_ids, program_row, dry_run, items_cache)

            # Sección Por Módulo
            for mod_row in module_rows:
                item_name_mod = f"{course_clean} — {group_name} — {mod_row['modulo']}"
                monday_upsert_item(board_id, group_modulo_id, item_name_mod,
                                   col_ids, mod_row, dry_run, items_cache)

            # Sección Por Encuesta (registros individuales)
            if group_encuesta_id:
                for enc_row in individual_rows:
                    alumno = enc_row.get("alumno") or "Anónimo"
                    item_name_enc = f"{course_clean} — {group_name} — {enc_row['modulo']} — {alumno}"
                    monday_upsert_item(board_id, group_encuesta_id, item_name_enc,
                                       col_ids, enc_row, dry_run, items_cache)

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
                raw = get_surveys_by_group(group_id, use_activities=True)
                if raw is None:
                    continue
                subjects_map = build_subjects_map(courses_data, course)
                program_row, module_rows, individual_rows = process_group_data(raw, group_name, course, subjects_map)
                monday_upsert_item(board_id, group_programa_id, f"{course_clean} — {group_name}",
                                   col_ids, program_row, dry_run, items_cache)
                for mod_row in module_rows:
                    monday_upsert_item(board_id, group_modulo_id,
                                       f"{course_clean} — {group_name} — {mod_row['modulo']}",
                                       col_ids, mod_row, dry_run, items_cache)
                if group_encuesta_id:
                    for enc_row in individual_rows:
                        alumno = enc_row.get("alumno") or "Anónimo"
                        monday_upsert_item(board_id, group_encuesta_id,
                                           f"{course_clean} — {group_name} — {enc_row['modulo']} — {alumno}",
                                           col_ids, enc_row, dry_run, items_cache)
                retried += 1
            except Exception as e2:
                print(f"    ✗ Reintento fallido para {group_name}: {e2}")
        if retried:
            print(f"  ✓ {retried} grupo(s) recuperado(s) en el reintento")

    print(f"\n  ✓ Sync completado {'(simulado)' if dry_run else ''}")

    # ── Paso final: enlazar "Cuenta empresa" por DNI ──────────────────────────
    if not dry_run:
        print(f"\n[{'2' if filter_course else '2'}] Enlazando 'Cuenta empresa' por DNI...")
        linker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_encuestas_alumnos.py")
        result = subprocess.run(
            [sys.executable, linker, "--board", "egh"],
            capture_output=True, text=True
        )
        # Mostrar resumen del resultado
        for line in result.stdout.splitlines():
            if any(k in line for k in ["✅", "⚠️", "❌", "📊 RESUMEN", "encontrada", "Errores", "empresa:"]):
                print(f"  {line.strip()}")
        if result.returncode != 0:
            print(f"  ⚠️  link_encuestas_alumnos.py terminó con error (código {result.returncode})")
            if result.stderr:
                print(f"  {result.stderr[:300]}")
        else:
            print(f"  ✓ Enlace de empresas completado")



# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sincronización EvolCampus → Monday.com")
    parser.add_argument("--explore",    action="store_true", help="Explorar estructura de datos (solo lectura)")
    parser.add_argument("--test-local", metavar="FILE",      help="Probar procesamiento sobre un JSON local")
    parser.add_argument("--setup",      action="store_true", help="Crear board en Monday (solo 1ª vez)")
    parser.add_argument("--sync",         action="store_true", help="Sincronizar datos")
    parser.add_argument("--dry-run",      action="store_true", help="Simular sync sin escribir en Monday")
    parser.add_argument("--filter-course",metavar="TEXTO",     help="Sincronizar solo grupos cuyo curso contenga este texto (ej: 'EGH')")
    parser.add_argument("--force", action="store_true", help="Ignora el salto 'sin cambios' por fecha y reprocesa todos los grupos (backfill).")
    args = parser.parse_args()

    if not any([args.explore, args.setup, args.sync, args.test_local]):
        parser.print_help()
        sys.exit(0)

    if args.explore:
        mode_explore()
    if args.test_local:
        mode_test_local(args.test_local)
    if args.setup:
        mode_setup()
    if args.sync:
        mode_sync(dry_run=args.dry_run, filter_course=args.filter_course, force=args.force)
