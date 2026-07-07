"""
monday_to_dashboard.py
──────────────────────────────────────────────────────────────────────────────
Lee el board de Monday "Encuestas: Prueba" y genera datos_dashboard.json
en el mismo formato que espera el dashboard.

Calcula automáticamente:
  - Sentimiento de cada comentario (positive / negative / neutral)
  - EGH Parte por módulo (Parte 1, 2, 3 según el número del módulo)
  - Métricas de grupo: overall_avg, last_survey_date,
    modules_with_responses, total_modules

Uso:
  python3 monday_to_dashboard.py
  python3 monday_to_dashboard.py --output ruta/datos.json

Requisitos:
  pip install requests
──────────────────────────────────────────────────────────────────────────────
"""

import requests
import json
import re
import sys
import os
import argparse
from datetime import datetime
from collections import defaultdict

# ── Mapa de temas EGH → Parte (cargado del Excel de estructura) ──────────────
def _build_egh_parte_map():
    """Lee el Excel de estructura del máster EGH.
    Devuelve ({tema_lower: parte_num}, {tema_lower: orden_global})
    donde orden_global es la posición del tema en el Excel (para ordenar módulos)."""
    try:
        import openpyxl
        xlsx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "Estructura máster",
                            "Control producción máster EGH_actualizado.xlsx")
        wb = openpyxl.load_workbook(xlsx, data_only=True)
        ws = wb["Estructura"]
        parte_map = {}
        order_map = {}
        current_parte_num = None
        order = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            val_a = str(row[0] or "").strip()
            if val_a.startswith("Parte 1."):
                current_parte_num = 1
            elif val_a.startswith("Parte 2."):
                current_parte_num = 2
            elif val_a.startswith("Parte 3."):
                current_parte_num = 3
            tema = str(row[2] or "").strip()
            if tema and current_parte_num:
                key = tema.lower()
                parte_map[key] = current_parte_num
                order_map[key] = order
                order += 1
        aliases = {
            "3.6. tecnología aplicada al huésped": 1,
        }
        parte_map.update(aliases)
        return parte_map, order_map
    except Exception as e:
        print(f"  ⚠️  No se pudo cargar el Excel de estructura EGH: {e}")
        return {}, {}

_EGH_PARTE_MAP, _EGH_ORDER_MAP = _build_egh_parte_map()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

# Credencial desde variable de entorno / GitHub Secret (nunca en código).
MONDAY_TOKEN = os.environ.get("MONDAY_TOKEN", "")
MONDAY_API   = "https://api.monday.com/v2"
BOARD_ID     = "5093144633"

if not MONDAY_TOKEN:
    print("✗ Falta la variable de entorno MONDAY_TOKEN.", file=sys.stderr)
    sys.exit(2)

GROUP_MODULO_ID   = "group_mm1dg1g7"
GROUP_PROGRAMA_ID = "group_mm1dw424"
GROUP_ENC_EGH_ID  = "group_mm20r2pn"   # Por Encuesta (alumno x módulo)

# Módulos que se deben omitir del dashboard (nombres en minúsculas, comparación parcial o exacta)
EXCLUDED_MODULES = {
    "¿qué opinas de nuestro programa?",
    # Nombres antiguos de módulos EGH sustituidos por versiones renombradas
    'descripción de turnos, procedimientos del "back" y normativa de front desk',
    "recepción corazón del hotel & departamento en la actualidad",
}

# Grupos que se deben omitir del dashboard (comparación exacta con el nombre del grupo)
EXCLUDED_GROUPS = {
    "PEHK - Programa Executive Housekeeping (2025/26)",
}

# IDs de columnas — Board EGH/CSUL (de monday_config.json)
COL = {
    "grupo":         "text_mm1d5xhk",
    "asignatura":    "text_mm1d7n1t",
    "modulo":        "text_mm1d5y4g",
    "formador":      "text_mm1dmy42",
    "pct_global":    "numeric_mm1dhhft",
    "respuestas":    "numeric_mm1dfn2z",
    "pct_formador":  "numeric_mm1dsfzx",
    "pct_contenido": "numeric_mm1dqaff",
    "pct_formato":   "numeric_mm1ds82j",
    "n_comentarios": "numeric_mm1dgcdw",
    "fecha_ultima":  "date_mm1da69v",
    "comentarios":   "long_text_mm1zjbs0",
    "fecha_inicio":  "date_mm1zmqf7",
    "fecha_fin":     "date_mm1zd8t3",
    # Formadores individuales (hasta 6)
    "formador_1": "text_mm26gnw8",  "pct_formador_1": "numeric_mm268c37",
    "formador_2": "text_mm26h5yh",  "pct_formador_2": "numeric_mm26w23t",
    "formador_3": "text_mm267ev5",  "pct_formador_3": "numeric_mm26nwmk",
    "formador_4": "text_mm26psxb",  "pct_formador_4": "numeric_mm265h6e",
    "formador_5": "text_mm266sca",  "pct_formador_5": "numeric_mm2639c5",
    "formador_6": "text_mm26sdgr",  "pct_formador_6": "numeric_mm26efs9",
    # Columnas del grupo "Por Encuesta"
    "cuenta_empresa": "text_mm2fka7y",
    "alumno_text":    "text_mm20aac4",
}

# ── Board de Cursos (Dirección Hotelera, Housekeeping, etc.) ──
CURSOS_BOARD_ID          = "5094417029"
CURSOS_GROUP_MODULO_ID   = "group_mm29fvs5"
CURSOS_GROUP_PROGRAMA_ID = "group_mm296ky1"
CURSOS_GROUP_ENC_ID      = "group_mm291wbp"   # Por Encuesta (alumno x módulo)

COL_CURSOS = {
    "grupo":         "text_mm29712e",
    "modulo":        "text_mm29djjz",
    "formador":      "text_mm29fpgq",
    "alumno":        "text_mm29myzy",
    "pct_global":    "numeric_mm296h1j",
    "respuestas":    "numeric_mm29kw10",
    "pct_formador":  "numeric_mm29526k",
    "pct_contenido": "numeric_mm29d7jj",
    "pct_formato":   "numeric_mm29vxvk",
    "n_comentarios": "numeric_mm29wkdd",
    "fecha_ultima":  "date_mm29za1g",
    "comentarios":   "long_text_mm29j0zr",
    "fecha_inicio":  "date_mm296gez",
    "fecha_fin":     "date_mm29vwxd",
    "formador_1": "text_mm2965ry",  "pct_formador_1": "numeric_mm29pws9",
    "formador_2": "text_mm29r10z",  "pct_formador_2": "numeric_mm29zmwb",
    "formador_3": "text_mm294wn",   "pct_formador_3": "numeric_mm29cbwm",
    "formador_4": "text_mm29ztzv",  "pct_formador_4": "numeric_mm29s2f",
    "formador_5": "text_mm29vcy7",  "pct_formador_5": "numeric_mm29qwst",
    "formador_6": "text_mm29t7dn",  "pct_formador_6": "numeric_mm29avf2",
    # Columnas del grupo "Por Encuesta"
    "cuenta_empresa": "text_mm2fc06a",
    "alumno_text":    "text_mm29myzy",
    # Columna de nombre de programa (para desambiguar cursos con mismo nombre)
    "programa":       "text_mm48ram",
}


# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS DE SENTIMIENTO
# ══════════════════════════════════════════════════════════════════════════════

_POSITIVE_WORDS = [
    "excelente", "perfecto", "genial", "fantástico", "fantástica", "increíble",
    "maravilloso", "maravillosa", "estupendo", "estupenda", "muy bueno", "muy buena",
    "muy bien", "espectacular", "brillante", "motivador", "motivadora", "dinámico",
    "dinámica", "muy práctico", "muy útil", "muy claro", "muy clara", "enriquecedor",
    "inspirador", "muy interesante", "muy completo", "me ha encantado", "me encantó",
    "me encanta", "muy profesional", "muy didáctico", "muy ameno", "muy entretenido",
    "muy buena explicación", "muy bien explicado", "todo perfecto", "10 de 10",
    "10/10", "notable", "sobresaliente", "top", "ideal", "muy satisfecho",
    "muy satisfecha", "recomendaría", "muy recomendable",
    "bien", "bueno", "buena", "interesante", "educativo", "educativa",
    "útil", "claro", "clara", "práctico", "práctica", "ameno", "amena",
    "correcto", "correcta", "completo", "completa", "organizado", "organizada",
    "profesional", "didáctico", "didáctica", "entretenido", "entretenida",
    "satisfecho", "satisfecha", "contento", "contenta", "aprendí", "aprendido",
    "bastante bien", "ha estado bien", "ha ido bien",
    "me ha gustado", "me gustó", "me gusta",
    # Expresiones de gratitud y valoración positiva
    "felicitar", "felicitaciones", "enhorabuena", "felicidades",
    "gracias por", "muchas gracias por", "los mejores", "lo mejor",
    "me ha ayudado", "me ayudó", "me ha servido", "me sirvió",
    "muy importante", "muy necesario", "muy necesaria", "fundamental",
    "muy valioso", "muy valiosa", "muy enriquecedor", "muy enriquecedora",
    "muy emocionante", "muy impactante", "muy revelador", "muy reveladora",
    "ha cambiado", "ha mejorado", "ha abierto", "me ha abierto",
    "recomiendo", "lo recomiendo", "muy recomendado", "muy recomendada",
    "felicidades", "enhorabuena", "sois los mejores", "eres el mejor",
    "eres la mejor", "el mejor curso", "la mejor formación",
    "imprescindible", "necesario", "necesaria", "clave",
]
_NEGATIVE_WORDS = [
    "malo", "mala", "pésimo", "pésima", "horrible", "terrible", "deficiente",
    "aburrido", "aburrida", "confuso", "confusa", "no se entiende", "no entendí",
    "no me gustó", "no me gusto", "demasiado rápido", "demasiado lento",
    "demasiado teórico", "muy rápido", "muy lento", "muy teórico",
    "repetitivo", "repetitiva", "insuficiente",
    "falta contenido", "falta tiempo", "falta profundidad", "falta material",
    "faltan temas", "faltan contenidos", "faltan ejemplos", "faltan materiales",
    "falta más", "faltan más",
    "mejorable", "poco claro", "poco clara", "desorganizado", "no aporta",
    "muy básico", "poco práctico", "no recomendaría", "difícil de seguir",
    "poco tiempo", "muy denso", "muy densa",
    "desactualizado", "desactualizada",
    "largo", "larga", "muy largo", "muy larga", "demasiado largo", "demasiado larga",
    # Problemas técnicos y de contenido
    "no funciona", "no se oía", "no se oye", "no se escucha", "no se ve",
    "no se lee", "no se leen", "no carga", "no abre",
    "lento", "lenta", "un poco lento", "un poco lenta",
    "innecesario", "innecesaria",
    "mal maquetado", "mal maquetada", "mal formateado",
    "no está completo", "no estar completo", "incompleto", "incompleta",
    "no sigue el formato", "costado mucho", "me ha costado",
    "forzado", "forzada", "demasiado forzado", "demasiado forzada",
    # Valoraciones claramente negativas
    "pérdida de tiempo", "perdida de tiempo", "perder el tiempo", "perder tiempo",
    "no sirve", "no sirve para nada", "no aporta nada", "sin valor",
    "muy malo", "muy mala", "pésimo", "pésima", "horrible", "fatal",
    "no me ha aportado", "no me aportó", "no he aprendido",
    "decepcionante", "decepcionado", "decepcionada",
    "no vale", "no merece", "no merece la pena",
]
_NEGATION_MARKERS = [
    'no ', 'no\xa0', 'nunca ', 'jamás ', 'tampoco ',
    'pensé', 'pensaba', 'creí', 'creía', 'esperaba', 'imaginé',
    'parecía', 'iba a ser',
    # Nota: "pero", "aunque", "sin embargo" se eliminan porque anulan incorrectamente
    # las palabras negativas que los siguen ("bien pero demasiado rápido" → negativo correcto)
]

def _is_negated(text_lower, match_start):
    preceding = text_lower[max(0, match_start - 55): match_start]
    return any(m in preceding for m in _NEGATION_MARKERS)

def analyze_sentiment(text):
    """Devuelve 'positive', 'negative' o 'neutral'."""
    if not text or not text.strip():
        return "neutral"
    t = text.lower()
    # Contar positivas (descartando las negadas: "no...bien" no cuenta)
    pos = 0
    for w in _POSITIVE_WORDS:
        for match in re.finditer(re.escape(w), t):
            if not _is_negated(t, match.start()):
                pos += 1
    # Contar negativas (descartando las negadas: "no es malo" no cuenta)
    neg = 0
    for w in _NEGATIVE_WORDS:
        for match in re.finditer(re.escape(w), t):
            if not _is_negated(t, match.start()):
                neg += 1
    if neg > 0 and pos > 0: return "neutral"   # mixto → neutro
    if pos > neg: return "positive"
    if neg > pos: return "negative"
    return "neutral"


# ══════════════════════════════════════════════════════════════════════════════
# DETECCIÓN DE "REQUIERE ACCIÓN"
# ══════════════════════════════════════════════════════════════════════════════

_ACTION_WORDS = [
    # Problemas y quejas
    "problema", "error", "fallo", "falta", "faltan", "no funciona", "no puedo",
    "solicito", "necesito", "urgente", "queja", "incidencia", "corregir",
    "cambiar", "mal", "peor", "inadecuado", "inadecuada", "inaceptable",
    "no sirve", "mejorar", "debería", "deberían", "por favor",
    "sin respuesta", "no responde", "no contesta",
    "insuficiente", "demasiado", "poco tiempo", "muy denso", "muy densa",
    "difícil de seguir", "no se entiende", "no entendí", "confuso", "confusa",
    "desorganizado", "desorganizada", "desactualizado", "desactualizada",
    "muy básico", "poco práctico", "poco clara", "poco claro",
    "no recomendaría", "no aporta", "decepcionante", "frustrante",
    # Sugerencias y feedback constructivo
    "podría", "podrían", "sería bueno", "sería mejor", "estaría bien",
    "sugiero", "sugerencia", "propongo", "recomiendo", "convendría",
    "se podría", "habría que", "faltaría", "añadir", "incluir",
    "resumir", "resumido", "ampliar", "profundizar", "actualizar",
    "no está completo", "no se lee", "no se leen", "mal maquetado",
    "formato descargable", "no sigue el formato", "parece no estar",
    "pero ", "aunque ", "sin embargo", "no obstante",
    "forzada", "forzado", "innecesario", "innecesaria",
]

def detect_action_needed(text):
    """Retorna True si el comentario parece requerir atención o seguimiento."""
    if not text or not text.strip():
        return False
    t = text.lower()
    if "?" in t:
        return True
    if any(w in t for w in _ACTION_WORDS):
        return True
    # Comentarios negativos siempre requieren acción
    if analyze_sentiment(text) == "negative":
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# EGH PARTE
# ══════════════════════════════════════════════════════════════════════════════

def _module_sort_key(module_name):
    """Ordena módulos por número ('Módulo 3:...' → 3). Módulos sin número van al final."""
    m = re.match(r'[Mm][oó]dulo\s+(\d+)', module_name)
    return (0, int(m.group(1))) if m else (1, module_name)

def _normalize_mod(s):
    """Normaliza un nombre de módulo para comparación: minúsculas, espacios normalizados."""
    s = str(s).strip().lower()
    # Normalizar número inicial sin espacio: "1.9.texto" -> "1.9. texto"
    s = re.sub(r'^(\d+\.\d+[a-z]?)\.(\S)', r'\1. \2', s)
    # Colapsar espacios múltiples
    s = re.sub(r'\s+', ' ', s)
    return s

def _norm_kw(s):
    """Normaliza para comparación de palabras clave: minúsculas, sin acentos."""
    s = s.lower()
    for a, b in [('á','a'),('à','a'),('ä','a'),('â','a'),('é','e'),('è','e'),('ë','e'),('ê','e'),
                 ('í','i'),('ì','i'),('ï','i'),('î','i'),('ó','o'),('ò','o'),('ö','o'),('ô','o'),
                 ('ú','u'),('ù','u'),('ü','u'),('û','u'),('ñ','n'),('ç','c')]:
        s = s.replace(a, b)
    return s


# Palabras clave por Parte, derivadas del Excel de estructura
_EGH_KW_P1 = [
    "gastronomia sostenible","naturaleza y el medio ambiente","economia circular",
    "transicion energetica","retos de la sostenibilidad","diseno de sostenibilidad",
    "comunicacion etica","generacion de valor","sostenibilidad social","rsc",
    "casos de exito en sostenibilidad","sostenibilidad en la aviacion",
    "marketing y promocion en sostenibilidad","la sostenibilidad en empresas",
    "la empresa y las personas","fundamentos de la digitalizacion","negocio vs tecnologia",
    "las personas, claves del exito","introduccion a la transformacion digital",
    "smart city","plan de digitalizacion","tecnologia aplicada",
    "autoconocimiento","marca personal","lider coach","liderazgo situacional",
    "liderazgo influyente","liderazgo transformador","inteligencia emocional",
    "liderazgo humanista","liderazgo disruptivo","liderazgo vocacional",
    "liderazgo integral","liderazgo sistemico","autoliderazgo",
    "gestion del absentismo","administracion del tiempo","experiencia del empleado",
    "liderazgo en la resolucion","liderazgo en entornos","liderazgo enfocado",
]
_EGH_KW_P3 = [
    "business plan","conceptualizacion","reposicionamiento","la vision de los ceo",
    "leyes, modelos de gestion","nuevos modelos de alojamiento",
    "herramientas colaborativas","base de datos y creacion","creacion de graficos",
    "creacion de presentaciones","ciberseguridad","herramientas de organizacion",
    "finanzas hoteleras","segmentacion de clientes","herramienta disc",
    "tecnicas de venta","plan y presupuesto de marketing","marketing partnership",
    "influencer marketing","revenue management","distribucion hotelera",
    "marketing relacional","segmento lujo","el golf","marketing experiencial",
    "negociacion b2b","marketing y comercializacion",
    "digitalizacion e inteligencia artificial para la comercializacion",
    "aleman","ingles","liderazgo intercultural","intergeneracional",
    "talento senior","talento joven","personas con discapacidad","menopausia","lgtb",
    "planificacion, atraccion","integracion, desarrollo","redes sociales",
]
_EGH_KW_P2 = [
    "front desk","reputacion online","e-wom","customer experience","guest experience",
    "ventas en front","ia aplicada","compras y gestion de stock","cocina","buffet","show cooking",
    "pasteleria","alimentacion responsable","bar y comedor","room service","coctel","smoothie",
    "cerveza","mundo de los vinos","oferta gastronomica","tematizacion",
    "appcc","seguridad alimentaria","calidad y esg","presupuestos hoteleros",
    "finanzas aplicadas al departamento","housekeeping","gobernanta","lavandera",
    "mice y bodas","safety","compliance en el hotel","higienico-sanitaria",
    "auditorias externas","habilidades y destrezas en las instalaciones",
    "operaciones basicas","procesos de cocina","procesos de bar","f&b","ttss",
    "mantenimiento","administracion","recepcion",
]


def egh_parte_from_module(module_name, course_name):
    """Devuelve (parte, order) para cursos EGH basándose en el Excel de estructura.
    parte  = 'Parte 1/2/3' o None
    order  = posición en el Excel (int) o None si no se encontró match exacto"""
    if not ("EGH" in (course_name or "") or "Hospitality" in (course_name or "")):
        return None, None

    key = _normalize_mod(module_name)

    # 1. Búsqueda exacta contra el mapa del Excel (normalizado)
    for map_key, parte_num in _EGH_PARTE_MAP.items():
        if _normalize_mod(map_key) == key:
            return f"Parte {parte_num}", _EGH_ORDER_MAP.get(map_key)

    # 2. Búsqueda por cobertura con prefijo numérico coincidente
    key_words = set(re.sub(r'[^\w\s]', '', key).split())
    best_match = None
    best_order = None
    best_score = 0
    for map_key, parte_num in _EGH_PARTE_MAP.items():
        mk = _normalize_mod(map_key)
        mk_words = set(re.sub(r'[^\w\s]', '', mk).split())
        if not mk_words:
            continue
        key_prefix = re.match(r'^\d+[\.\d]*', key)
        mk_prefix  = re.match(r'^\d+[\.\d]*', mk)
        if key_prefix and mk_prefix and key_prefix.group() != mk_prefix.group():
            continue
        num_token = re.sub(r'[^\w]', '', key.split('.')[0]) if '.' in key else ''
        content_words = key_words - {num_token} if num_token else key_words
        if not content_words:
            continue
        coverage = len(content_words & mk_words) / len(content_words)
        if coverage > best_score and coverage >= 0.75:
            best_score = coverage
            best_match = parte_num
            best_order = _EGH_ORDER_MAP.get(map_key)

    if best_match:
        return f"Parte {best_match}", best_order

    # 3. Fallback por palabras clave — sin orden exacto del Excel
    mod_n = _norm_kw(module_name)
    if any(kw in mod_n for kw in _EGH_KW_P3):
        return "Parte 3", None
    if any(kw in mod_n for kw in _EGH_KW_P2):
        return "Parte 2", None
    if any(kw in mod_n for kw in _EGH_KW_P1):
        return "Parte 1", None

    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# MONDAY API
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
    # Reintentos ante timeouts / cortes de red / 429-5xx (los boards son grandes).
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


def fetch_all_items(board_id):
    """Obtiene todos los ítems del board con paginación automática."""
    # Primera página
    q_first = """
    query($board_id: ID!) {
      boards(ids: [$board_id]) {
        items_page(limit: 500) {
          cursor
          items {
            id name
            group { id }
            column_values { id text value }
          }
        }
      }
    }
    """
    data      = monday_query(q_first, {"board_id": board_id})
    page      = data["boards"][0]["items_page"]
    all_items = list(page["items"])
    cursor    = page.get("cursor")

    # Páginas siguientes
    q_next = """
    query($cursor: String!) {
      next_items_page(limit: 500, cursor: $cursor) {
        cursor
        items {
          id name
          group { id }
          column_values { id text value }
        }
      }
    }
    """
    while cursor:
        data      = monday_query(q_next, {"cursor": cursor})
        page      = data["next_items_page"]
        all_items.extend(page["items"])
        cursor    = page.get("cursor")
        print(f"  ...{len(all_items)} ítems cargados")

    return all_items


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE LECTURA DE COLUMNAS
# ══════════════════════════════════════════════════════════════════════════════

_PAFB_BASE = "Programa Avanzado de Alimentos y Bebidas"
_PALH_BASE = "Programa Avanzado de Liderazgo Hotelero"

def _classify_pafb(course_name, group_name):
    """Clasifica grupos PAFB en (2025) o (Actualizado) según el nombre del grupo.
    Solo actúa cuando course_name es el nombre base genérico (sin sufijo de año).
    Promos 56 y 59-20/10 → 2025. Promos 59-15/10, 60, 65 → Actualizado."""
    if not course_name.startswith(_PAFB_BASE):
        return course_name
    import re
    m = re.search(r'[Pp]romo\s+(\d+)', group_name)
    if not m:
        return course_name
    promo = int(m.group(1))
    if promo == 56:
        return f"{_PAFB_BASE} (2025)"
    if promo == 59:
        if "20/10" in group_name:
            return f"{_PAFB_BASE} (2025)"
        if "15/10" in group_name:
            return f"{_PAFB_BASE} (Actualizado)"
    if promo in (60, 65):
        return f"{_PAFB_BASE} (Actualizado)"
    return course_name

def _classify_palh(course_name, group_name):
    """Clasifica PALH (40 horas) vs PAL (66 horas) según prefijo del grupo.
    Solo actúa sobre el nombre base genérico sin sufijo de horas."""
    if not course_name.startswith(_PALH_BASE):
        return course_name
    if group_name.startswith("PALH"):
        return f"{_PALH_BASE} (40 horas)"
    if group_name.startswith("PAL ") or group_name.startswith("PAL-"):
        return f"{_PALH_BASE} (66 horas)"
    return course_name

def _cv_map(item):
    """Devuelve {col_id: {text, value}} para un ítem."""
    return {cv["id"]: cv for cv in item.get("column_values", [])}

def col_text(cv_map, col_id):
    cv = cv_map.get(col_id, {})
    return cv.get("text") or ""

def col_date(cv_map, col_id):
    """Devuelve la fecha en formato YYYY-MM-DD."""
    cv = cv_map.get(col_id, {})
    raw = cv.get("value")
    if raw:
        try:
            return json.loads(raw).get("date", "")
        except Exception:
            pass
    return cv.get("text") or ""

def col_comments(cv_map, col_id):
    """Devuelve la lista [{text, student}] del campo long_text.
    Intenta dos vías: el campo 'value' (JSON anidado) y el campo 'text' (JSON directo)."""
    cv = cv_map.get(col_id, {})

    # Vía 1: campo 'text' — Monday devuelve el JSON directamente aquí para long_text
    raw_text = cv.get("text", "")
    if raw_text and raw_text.strip().startswith("["):
        try:
            result = json.loads(raw_text)
            if isinstance(result, list):
                return result
        except Exception:
            pass

    # Vía 2: campo 'value' — envoltura {"text": "[...]"}
    raw = cv.get("value")
    if raw:
        try:
            inner_text = json.loads(raw).get("text", "")
            if inner_text:
                return json.loads(inner_text)
        except Exception:
            pass

    return []

def to_float(val):
    try:    return float(val) if val else None
    except: return None

def iso_to_display(date_iso):
    """YYYY-MM-DD → DD/MM/YYYY"""
    if not date_iso:
        return ""
    try:
        return datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return date_iso


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DEL JSON DEL DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def _process_board_items(items, col, group_modulo_id, group_enc_id, group_programa_id, prog_index, groups_map):
    """Procesa items de un board y los añade a prog_index y groups_map."""

    modulo_items   = [i for i in items if i["group"]["id"] == group_modulo_id]
    enc_items      = [i for i in items if i["group"]["id"] == group_enc_id]
    programa_items = [i for i in items if i["group"]["id"] == group_programa_id]

    # ── Mapa de comentarios desde ítems "Por Encuesta" ──────────────────────
    # Monday trunca long_text a 2000 chars: módulos con 20+ comentarios quedan
    # con JSON inválido. Los ítems por encuesta tienen 1 comentario cada uno y
    # nunca se truncan; los agregamos aquí para reemplazar la lectura del módulo.
    enc_comments_map = defaultdict(list)
    for enc_item in enc_items:
        cvm    = _cv_map(enc_item)
        parts  = enc_item["name"].split(" — ", 3)
        course = col_text(cvm, col.get("programa", "")) or (parts[0].strip() if parts else "")
        group  = parts[1].strip() if len(parts) > 1 else ""
        course = _classify_pafb(course, group)
        course = _classify_palh(course, group)
        module = col_text(cvm, col["modulo"]) or (parts[2].strip() if len(parts) > 2 else "")
        if not module or module.strip().lower() in EXCLUDED_MODULES:
            continue
        for c in col_comments(cvm, col["comentarios"]):
            if c.get("text"):
                enc_comments_map[(course, group, module)].append(c)

    # ── Índice de datos de programa (fechas, n_total) por (curso, grupo) ──
    for item in programa_items:
        cvm    = _cv_map(item)
        parts       = item["name"].split(" — ", 1)
        course_name = col_text(cvm, col.get("programa", "")) or parts[0].strip()
        group_name  = parts[1].strip() if len(parts) > 1 else ""
        course_name = _classify_pafb(course_name, group_name)
        course_name = _classify_palh(course_name, group_name)
        if group_name in EXCLUDED_GROUPS:
            continue
        prog_index[(course_name, group_name)] = {
            "fecha_inicio":        col_date(cvm, col.get("fecha_inicio", "")),
            "fecha_fin":           col_date(cvm, col.get("fecha_fin", "")),
            "n_total":             int(float(col_text(cvm, col["respuestas"]) or 0)),
            "last_survey_date_iso": col_date(cvm, col["fecha_ultima"]),
        }

    # ── Agrupar actividades por curso → grupo ──
    for item in modulo_items:
        cvm   = _cv_map(item)
        parts = item["name"].split(" — ", 2)
        if len(parts) == 3:
            course_name = col_text(cvm, col.get("programa", "")) or parts[0].strip()
            group_name  = parts[1].strip()
        else:
            grupo_raw   = col_text(cvm, col["grupo"])
            course_name = col_text(cvm, col.get("programa", "")) or (parts[0].strip() if parts else item["name"])
            group_name  = grupo_raw or (parts[1].strip() if len(parts) > 1 else "")
        course_name = _classify_pafb(course_name, group_name)
        course_name = _classify_palh(course_name, group_name)

        if group_name in EXCLUDED_GROUPS:
            continue
        module_name = col_text(cvm, col["modulo"])
        if module_name.strip().lower() in EXCLUDED_MODULES:
            continue
        n           = int(float(col_text(cvm, col["respuestas"]) or 0))
        fecha_ult   = col_date(cvm, col["fecha_ultima"])

        raw_comments = (enc_comments_map.get((course_name, group_name, module_name))
                        or col_comments(cvm, col["comentarios"]))
        comments_out = [
            {
                "text":            c["text"],
                "student":         c.get("student", ""),
                "date":            iso_to_display(c.get("date", "")),
                "sentiment":       analyze_sentiment(c["text"]),
                "requires_action": detect_action_needed(c["text"]),
            }
            for c in raw_comments if c.get("text")
        ]

        parte, egh_order = egh_parte_from_module(module_name, course_name)
        global_pct  = to_float(col_text(cvm, col["pct_global"]))
        content_pct = to_float(col_text(cvm, col["pct_contenido"]))
        format_pct  = to_float(col_text(cvm, col["pct_formato"]))

        individual_trainers = []
        for i in range(1, 7):
            fkey = f"formador_{i}"
            pkey = f"pct_formador_{i}"
            if fkey in col and pkey in col:
                fname = col_text(cvm, col[fkey])
                fpct  = to_float(col_text(cvm, col[pkey]))
                if fname and fname.strip():
                    individual_trainers.append((fname.strip(), fpct))

        if individual_trainers:
            for idx, (fname, fpct) in enumerate(individual_trainers):
                act = {
                    "module":      module_name,
                    "trainer":     fname,
                    "n":           n if idx == 0 else 0,
                    "trainer_pct": fpct,
                    "content_pct": content_pct if idx == 0 else None,
                    "format_pct":  format_pct  if idx == 0 else None,
                    "global_pct":  global_pct,
                    "comments":    comments_out if idx == 0 else [],
                    "last_date":   iso_to_display(fecha_ult),
                }
                if idx > 0:
                    act["n_count"] = False
                if parte:
                    act["egh_parte"] = parte
                if egh_order is not None:
                    act["egh_order"] = egh_order
                groups_map[course_name][group_name].append(act)
        else:
            activity = {
                "module":      module_name,
                "trainer":     col_text(cvm, col["formador"]),
                "n":           n,
                "trainer_pct": to_float(col_text(cvm, col["pct_formador"])),
                "content_pct": content_pct,
                "format_pct":  format_pct,
                "global_pct":  global_pct,
                "comments":    comments_out,
                "last_date":   iso_to_display(fecha_ult),
            }
            if parte:
                activity["egh_parte"] = parte
            if egh_order is not None:
                activity["egh_order"] = egh_order
            groups_map[course_name][group_name].append(activity)


def _extract_empresa(items, col, group_enc_id, module_companies, student_empresa, all_companies):
    """Extrae empresa de items del grupo 'Por Encuesta' y construye los mapas de empresa."""
    enc_col = col.get("cuenta_empresa", "")
    alu_col = col.get("alumno_text") or col.get("alumno", "")
    for item in items:
        if item["group"]["id"] != group_enc_id:
            continue
        cvm     = _cv_map(item)
        empresa = col_text(cvm, enc_col).strip()
        if not empresa:
            continue
        parts       = item["name"].split(" — ", 3)
        course_name = parts[0].strip() if parts else ""
        group_name  = parts[1].strip() if len(parts) > 1 else ""
        mod_name    = parts[2].strip() if len(parts) > 2 else ""
        student     = col_text(cvm, alu_col).strip() or (parts[3].strip() if len(parts) > 3 else "")

        mod_key = (course_name, group_name, mod_name)
        if mod_key not in module_companies:
            module_companies[mod_key] = set()
        module_companies[mod_key].add(empresa)

        if student:
            stu_key = (course_name, group_name, student.lower())
            student_empresa[stu_key] = empresa

        all_companies.add(empresa)


def build_dashboard_json(items, cursos_items=None):
    """Reconstruye la estructura courses → groups → activities del dashboard.
    items: items del board EGH/CSUL
    cursos_items: items del board de Cursos (opcional)
    """
    prog_index       = {}
    groups_map       = defaultdict(lambda: defaultdict(list))
    all_companies    = set()
    module_companies = {}   # (course, group, module) -> set of empresa
    student_empresa  = {}   # (course, group, student_lower) -> empresa

    # Extraer empresa de items "Por Encuesta"
    _extract_empresa(items, COL, GROUP_ENC_EGH_ID, module_companies, student_empresa, all_companies)
    if cursos_items:
        _extract_empresa(cursos_items, COL_CURSOS, CURSOS_GROUP_ENC_ID, module_companies, student_empresa, all_companies)

    # Procesar board EGH/CSUL
    _process_board_items(items, COL, GROUP_MODULO_ID, GROUP_ENC_EGH_ID, GROUP_PROGRAMA_ID, prog_index, groups_map)

    # Procesar board de Cursos (si hay)
    if cursos_items:
        _process_board_items(cursos_items, COL_CURSOS, CURSOS_GROUP_MODULO_ID, CURSOS_GROUP_ENC_ID, CURSOS_GROUP_PROGRAMA_ID, prog_index, groups_map)

    # ── Construir estructura final ──
    courses_out = {}
    for course_name, groups in groups_map.items():
        courses_out[course_name] = {}
        for group_name, activities in groups.items():
            prog = prog_index.get((course_name, group_name), {})

            acts_with_n            = [a for a in activities if a["n"] > 0]
            total_modules          = len(activities)
            modules_with_responses = len(acts_with_n)

            # overall_avg: media ponderada por nº de respuestas
            if acts_with_n:
                total_weight = sum(a["n"] for a in acts_with_n)
                weighted_sum = sum(
                    a["global_pct"] * a["n"]
                    for a in acts_with_n
                    if a["global_pct"] is not None
                )
                overall_avg = round(weighted_sum / total_weight, 1) if total_weight else None
            else:
                overall_avg = None

            # last_survey_date: la fecha más reciente entre módulos y programa
            all_dates = [a["last_date"] for a in activities if a.get("last_date")]
            prog_last = prog.get("last_survey_date_iso")
            if prog_last:
                all_dates.append(iso_to_display(prog_last))
            last_survey_date = ""
            if all_dates:
                try:
                    last_survey_date = max(
                        all_dates,
                        key=lambda d: datetime.strptime(d, "%d/%m/%Y") if d else datetime.min
                    )
                except Exception:
                    last_survey_date = all_dates[-1]

            # Adjuntar empresas y empresa por comentario a cada actividad
            for act in activities:
                mod_key = (course_name, group_name, act["module"])
                act["companies"] = sorted(module_companies.get(mod_key, set()))
                for c in act.get("comments", []):
                    if c.get("student") and "empresa" not in c:
                        stu_key = (course_name, group_name, c["student"].lower())
                        c["empresa"] = student_empresa.get(stu_key, "")

            activities_sorted = sorted(activities, key=lambda a: _module_sort_key(a["module"]))
            courses_out[course_name][group_name] = {
                "group_name":             group_name,
                "course_name":            course_name,
                "start_date":             iso_to_display(prog.get("fecha_inicio", "")),
                "end_date":               iso_to_display(prog.get("fecha_fin", "")),
                "overall_avg":            overall_avg,
                "n_total":                prog.get("n_total", 0),
                "last_survey_date":       last_survey_date,
                "modules_with_responses": modules_with_responses,
                "total_modules":          total_modules,
                "activities":             activities_sorted,
            }

    return {
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "courses":   courses_out,
        "companies": sorted(all_companies, key=lambda s: s.lower()),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SINCRONIZACIÓN AL BOARD DE GESTIÓN DE COMENTARIOS
# ══════════════════════════════════════════════════════════════════════════════

import hashlib
import time

TRACKING_BOARD_ID = "5094293806"

TRACKING_COL = {
    "curso":           "text_mm27jjry",
    "grupo":           "text_mm27k5q4",
    "modulo":          "text_mm27xwta",
    "alumno":          "text_mm27zzhx",
    "comentario":      "long_text_mm27g5mj",
    "estado":          "color_mm2739ch",
    "responsable":     "text_mm27hvwa",
    "sentimiento":     "color_mm278p6e",
    "requiere_accion": "boolean_mm27brc9",
    "fecha_deteccion": "date_mm27y3f8",
    "hash_id":         "text_mm27be5f",
    "accion":          "text_mm2797ee",
}

def _comment_hash(curso, grupo, modulo, alumno, texto):
    """SHA-256 truncado a 16 hex para identificar un comentario único."""
    raw = f"{curso}||{grupo}||{modulo}||{alumno}||{texto}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _identity_hash(curso, grupo, modulo, alumno):
    """Hash de identidad: identifica un comentario por alumno+módulo+grupo (sin el texto).
    Permite encontrar el item existente aunque el texto haya cambiado levemente."""
    raw = f"{curso}||{grupo}||{modulo}||{alumno}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_tracking_source_from_items(items, cursos_items=None):
    """Construye el origen de datos para el tracking board leyendo directamente
    de los ítems 'Por Encuesta' de EGH y Cursos, sin pasar por el dashboard JSON.
    Devuelve un dict compatible con sync_comments_to_tracking_board."""
    mod_comments = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    def _process(enc_items, col):
        for item in enc_items:
            cvm    = _cv_map(item)
            parts  = item["name"].split(" — ", 3)
            course = parts[0].strip() if parts else ""
            group  = parts[1].strip() if len(parts) > 1 else ""
            module = col_text(cvm, col["modulo"]) or (parts[2].strip() if len(parts) > 2 else "")
            if not course or not module:
                continue
            for c in col_comments(cvm, col["comentarios"]):
                if not c.get("text"):
                    continue
                mod_comments[course][group][module].append({
                    "text":            c["text"],
                    "student":         c.get("student", ""),
                    "date":            c.get("date", ""),
                    "sentiment":       analyze_sentiment(c["text"]),
                    "requires_action": detect_action_needed(c["text"]),
                })

    _process([i for i in items if i["group"]["id"] == GROUP_ENC_EGH_ID], COL)
    if cursos_items:
        _process([i for i in cursos_items if i["group"]["id"] == CURSOS_GROUP_ENC_ID], COL_CURSOS)

    courses_out = {}
    for course, groups in mod_comments.items():
        courses_out[course] = {}
        for group, modules in groups.items():
            activities = []
            for module, comments in modules.items():
                dates = sorted([c["date"] for c in comments if c.get("date")])
                last_display = iso_to_display(dates[-1]) if dates else ""
                activities.append({"module": module, "comments": comments, "last_date": last_display})
            courses_out[course][group] = {"activities": activities}

    return {"courses": courses_out}


def sync_comments_to_tracking_board(dashboard_data, tracking_board_id=None):
    """Sincroniza comentarios al board de gestión con lógica UPSERT:
    - Si el comentario ya existe (por identidad alumno+módulo+grupo) → actualiza sentimiento,
      requiere_acción y texto, pero NO toca estado/responsable/acción (gestionados manualmente).
    - Si es nuevo → crea el item con estado 'Nuevo'.
    """
    board_id = tracking_board_id or TRACKING_BOARD_ID
    today = datetime.now().strftime("%Y-%m-%d")

    SENTIMENT_LABELS = {"positive": "Positivo", "negative": "Negativo", "neutral": "Neutro"}

    print(f"\n[SYNC] Leyendo board de tracking {board_id}...")
    existing_items = fetch_all_items(board_id)

    # Mapa: identity_hash → {id, sig} donde sig detecta cambios reales
    identity_to_item = {}
    for item in existing_items:
        cvm = _cv_map(item)
        curso_v  = col_text(cvm, TRACKING_COL["curso"])
        grupo_v  = col_text(cvm, TRACKING_COL["grupo"])
        modulo_v = col_text(cvm, TRACKING_COL["modulo"])
        alumno_v = col_text(cvm, TRACKING_COL["alumno"])
        if curso_v and alumno_v:
            ih = _identity_hash(curso_v, grupo_v, modulo_v, alumno_v)
            # La casilla "requiere acción" viene como texto "v"/"" en el board;
            # la normalizamos a "true"/"false" para que el sig case con el que
            # genera el upsert y los ítems sin cambios se SALTEN (no re-update).
            _chk = "true" if col_text(cvm, TRACKING_COL["requiere_accion"]).strip() else "false"
            identity_to_item[ih] = {
                "id":  item["id"],
                "sig": "{}||{}||{}".format(
                    col_text(cvm, TRACKING_COL["comentario"]),
                    col_text(cvm, TRACKING_COL["sentimiento"]),
                    _chk,
                ),
            }

    print(f"  ✓ {len(existing_items)} comentarios ya en el board")

    # Recopilar todos los comentarios del dashboard
    to_process = []
    for course_name, groups in dashboard_data.get("courses", {}).items():
        for group_name, group_data in groups.items():
            for act in group_data.get("activities", []):
                for c in act.get("comments", []):
                    if not c.get("text"):
                        continue
                    ih = _identity_hash(course_name, group_name,
                                        act.get("module", ""), c.get("student", ""))
                    ch = _comment_hash(course_name, group_name,
                                       act.get("module", ""), c.get("student", ""), c["text"])
                    fecha_mod = ""
                    raw_date = act.get("last_date", "")
                    if raw_date:
                        parts = raw_date.split("/")
                        if len(parts) == 3:
                            fecha_mod = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    to_process.append({
                        "curso":    course_name,
                        "grupo":    group_name,
                        "modulo":   act.get("module", ""),
                        "alumno":   c.get("student", ""),
                        "texto":    c["text"],
                        "sentiment": c.get("sentiment", "neutral"),
                        "requires_action": c.get("requires_action", False),
                        "identity_hash": ih,
                        "content_hash":  ch,
                        "fecha":    fecha_mod,
                    })

    created = updated = skipped = 0
    mut_update = """
    mutation($board_id: ID!, $item_id: ID!, $col_values: JSON!) {
      change_multiple_column_values(board_id: $board_id, item_id: $item_id, column_values: $col_values) { id }
    }
    """
    mut_create = """
    mutation($board_id: ID!, $item_name: String!, $column_values: JSON!) {
      create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) { id }
    }
    """

    for i, c in enumerate(to_process, 1):
        item_name = c["texto"][:50] + ("..." if len(c["texto"]) > 50 else "")
        sentiment_label = SENTIMENT_LABELS.get(c["sentiment"], "Neutro")

        existing = identity_to_item.get(c["identity_hash"])

        if existing:
            existing_id = existing["id"]
            req_str = "true" if c["requires_action"] else "false"
            new_sig = "{}||{}||{}".format(c["texto"], sentiment_label, req_str)
            if new_sig == existing["sig"]:
                skipped += 1
                continue  # sin cambios, no llamar a la API
            col_vals = json.dumps({
                TRACKING_COL["comentario"]:      {"text": c["texto"]},
                TRACKING_COL["sentimiento"]:     {"label": sentiment_label},
                TRACKING_COL["requiere_accion"]: {"checked": req_str},
                TRACKING_COL["hash_id"]:         c["identity_hash"],
            })
            try:
                monday_query(mut_update, {"board_id": board_id, "item_id": existing_id, "col_values": col_vals})
                updated += 1
            except Exception as e:
                print(f"  ✗ Error actualizando {item_name}: {e}")
        else:
            # CREAR nuevo
            col_vals = json.dumps({
                TRACKING_COL["curso"]:           c["curso"],
                TRACKING_COL["grupo"]:           c["grupo"],
                TRACKING_COL["modulo"]:          c["modulo"],
                TRACKING_COL["alumno"]:          c["alumno"],
                TRACKING_COL["comentario"]:      {"text": c["texto"]},
                TRACKING_COL["estado"]:          {"label": "Nuevo"},
                TRACKING_COL["sentimiento"]:     {"label": sentiment_label},
                TRACKING_COL["requiere_accion"]: {"checked": "true" if c["requires_action"] else "false"},
                TRACKING_COL["fecha_deteccion"]: {"date": c.get("fecha") or today},
                TRACKING_COL["hash_id"]:         c["identity_hash"],
            })
            try:
                result = monday_query(mut_create, {
                    "board_id": board_id,
                    "item_name": item_name,
                    "column_values": col_vals,
                })
                new_id = result.get("create_item", {}).get("id")
                if new_id:
                    # Guardar como dict {id, sig} — igual que las entradas leídas del
                    # board — para que un 2º comentario con la misma identidad
                    # (alumno+módulo+grupo) haga update y no rompa con existing["id"].
                    _req = "true" if c["requires_action"] else "false"
                    identity_to_item[c["identity_hash"]] = {
                        "id":  new_id,
                        "sig": "{}||{}||{}".format(c["texto"], sentiment_label, _req),
                    }
                created += 1
            except Exception as e:
                print(f"  ✗ Error creando {item_name}: {e}")
        time.sleep(0.3)

    print(f"  ✓ {created} nuevos · {updated} actualizados · {skipped} sin cambios — total: {len(to_process)}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monday → datos_dashboard.json")
    parser.add_argument("--output", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "datos_dashboard.json"),
                        help="Ruta del archivo JSON de salida")
    parser.add_argument("--sync-tracking", action="store_true",
                        help="Sincronizar comentarios nuevos al board de gestión de comentarios")
    parser.add_argument("--tracking-board", default=TRACKING_BOARD_ID,
                        help=f"ID del board de tracking (por defecto: {TRACKING_BOARD_ID})")
    args = parser.parse_args()

    print(f"\n── MONDAY → DASHBOARD ─────────────────────────────────────")

    print(f"[1] Leyendo ítems del board EGH/CSUL ({BOARD_ID})...")
    items = fetch_all_items(BOARD_ID)
    print(f"  ✓ {len(items)} ítems obtenidos")

    print(f"[1b] Leyendo ítems del board Cursos ({CURSOS_BOARD_ID})...")
    cursos_items = fetch_all_items(CURSOS_BOARD_ID)
    print(f"  ✓ {len(cursos_items)} ítems obtenidos")

    # v60cj · En CI (GitHub Actions) SKIP_DASHBOARD_REGEN=1 → solo sincronizar el
    # board de tracking. El JSON del dashboard lo reconstruye Netlify, y la
    # inyección en HTML locales no aplica en CI.
    _ci = os.environ.get("SKIP_DASHBOARD_REGEN", "") in ("1", "true", "yes")

    data = None
    if not _ci:
        print(f"[2] Construyendo estructura del dashboard...")
        data = build_dashboard_json(items, cursos_items=cursos_items)
        n_courses = len(data["courses"])
        n_groups  = sum(len(g) for g in data["courses"].values())
        n_acts    = sum(
            len(grp["activities"])
            for course in data["courses"].values()
            for grp in course.values()
        )
        print(f"  ✓ {n_courses} curso(s) · {n_groups} grupo(s) · {n_acts} actividades")

        print(f"[3] Guardando {args.output}...")
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        size_kb = len(json.dumps(data, ensure_ascii=False).encode()) // 1024
        print(f"  ✓ Guardado ({size_kb} KB) — generado: {data['generated']}")

    if args.sync_tracking:
        print(f"\n[·] Sincronizando comentarios al board de tracking...")
        tracking_source = build_tracking_source_from_items(items, cursos_items)
        sync_comments_to_tracking_board(tracking_source, args.tracking_board)

    if _ci:
        print()
        sys.exit(0)   # en CI no se inyecta en HTML locales

    # ── Inyectar JSON en los dashboards HTML (solo local) ─────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    html_paths = [
        os.path.join(script_dir, "..", "index", "index.html"),                    # Netlify (producción)
        os.path.join(script_dir, "..", "Dashboard base Monday - EGH", "dashboard.html"),  # local
    ]
    step = '5' if args.sync_tracking else '4'
    print(f"\n[{step}] Inyectando datos en dashboards HTML...")
    new_json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))

    def inject_html(path):
        if not os.path.exists(path):
            print(f"  ⚠️  No encontrado: {path}")
            return
        try:
            with open(path, encoding='utf-8') as f:
                html = f.read()
            marker = 'let DATA = '
            idx = html.find(marker)
            if idx == -1:
                print(f"  ⚠️  No se encontró 'let DATA =' en {os.path.basename(path)}")
                return
            start = idx + len(marker)
            depth = 0
            pos = start
            for i, ch in enumerate(html[start:]):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        pos = start + i + 1
                        break
            html_new = html[:start] + new_json_str + html[pos:]
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html_new)
            print(f"  ✓ {os.path.basename(path)} actualizado")
        except Exception as e:
            print(f"  ⚠️  Error inyectando {os.path.basename(path)}: {e}")

    for hp in html_paths:
        inject_html(hp)

    print()
