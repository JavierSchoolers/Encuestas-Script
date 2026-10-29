"""
Enlaza la columna 'Formador (rel)' (connect boards -> tablero Formadores) a partir
de la columna de texto 'Formador' en los tableros de encuestas.

Uso:
  # Backfill (reanudable: solo toca elementos con 'Formador (rel)' vacío)
  MONDAY_TOKEN=xxx python formador_link.py --max-minutes 340 --per-minute 200

  # Vaciar 'Formador (rel)' en las filas de alumnos ("Por Encuesta") de un tablero.
  # 2026-10-29: el backfill llenó ~31.700 filas del tablero Cursos y agotó su límite de
  # elementos conectados de monday; desde el 22-oct el sync no puede enlazar encuestas
  # con su matrícula. Nada usa esta columna en esas filas.
  MONDAY_TOKEN=xxx python formador_link.py --clear --boards 5094417029 --max-minutes 340

  # Desde el sync, para elementos nuevos:
  from formador_link import FormadorResolver, BOARDS
  resolver = FormadorResolver(gql)          # gql = tu función de llamada a la API
  ids = resolver.ids_for("Pablo Casado / Xavi López")   # -> [1386573..., ...]
  column_values[BOARDS[board_id]["rel"]] = {"item_ids": ids}
"""
import argparse
import collections
import json
import os
import re
import sys
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests

API_URL = "https://api.monday.com/v2"
FORMADORES_BOARD = 1381074797
PRIMARY_GROUP = "Formadores programas online"

# board_id -> columna texto, columna relación, grupo(s) a procesar
BOARDS = {
    5093144633: {"name": "Encuestas: EGH", "text": "text_mm1dmy42",
                 "rel": "board_relation_mm8bxa7x", "groups": ["group_mm20r2pn"]},
    5094417029: {"name": "Encuestas: Programas Schôolers", "text": "text_mm29fpgq",
                 "rel": "board_relation_mm8bvyy0", "groups": ["group_mm291wbp"]},
    5100940645: {"name": "Encuestas: Subvenciones RSK y AEHCOS", "text": "text_mm29fpgq",
                 "rel": "board_relation_mm8btca5", "groups": ["group_mm291wbp"]},
}

# Nombres del texto que no se pueden resolver automáticamente -> nombre exacto en Formadores
OVERRIDES = {
    "toni ferr": "Toni Ferrà Bonet",
    "ana mari garcia": "Ana María García Castro",
}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def make_gql(token):
    local = threading.local()  # una sesión HTTP por hilo

    def session():
        if not hasattr(local, "s"):
            local.s = requests.Session()
            local.s.headers.update({"Authorization": token, "API-Version": "2025-04",
                                    "Content-Type": "application/json"})
        return local.s

    def gql(query, variables=None, tries=8):
        for _ in range(tries):
            try:
                r = session().post(API_URL, json={"query": query, "variables": variables or {}}, timeout=120)
                body = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(15)
                continue
            errs = body.get("errors") or body.get("error_message")
            if errs:
                msg = json.dumps(errs)
                if r.status_code == 429 or "omplexity" in msg or "RATE_LIMIT" in msg or "Rate Limit" in msg:
                    m = re.search(r'"retry_in_seconds":\s*(\d+)', msg) or re.search(r"(\d+)\s*seconds", msg)
                    time.sleep(int(m.group(1)) + 2 if m else 30)
                    continue
                raise RuntimeError(msg)
            return body["data"]
        raise RuntimeError("Demasiados reintentos contra la API de monday")
    return gql


class FormadorResolver:
    """Convierte 'Nombre Apellido / Nombre Apellido' en ids del tablero Formadores."""

    def __init__(self, gql):
        data = gql("{boards(ids:[%d]){items_page(limit:500){cursor items{id name group{title}}}}}"
                   % FORMADORES_BOARD)["boards"][0]["items_page"]
        items, cursor = data["items"], data["cursor"]
        while cursor:
            data = gql("query($c:String!){next_items_page(limit:500,cursor:$c){cursor items{id name group{title}}}}",
                       {"c": cursor})["next_items_page"]
            items += data["items"]
            cursor = data["cursor"]
        self.fm = [(int(i["id"]), i["name"], norm(i["name"]).split(), i["group"]["title"]) for i in items]
        self.by_name = {f[1]: f for f in self.fm}
        self.cache = {}
        self.unmatched = collections.Counter()

    def resolve(self, name):
        t = norm(name).split()
        key = " ".join(t)
        if key in self.cache:
            return self.cache[key]
        res = None
        if key in OVERRIDES and OVERRIDES[key] in self.by_name:
            res = self.by_name[OVERRIDES[key]][0]
        elif t:
            cand = [f for f in self.fm if all(x in f[2] for x in t)]
            if not cand and len(t) >= 2:  # 'Han Gaviria' -> 'Hanlly Gaviria Toro'
                cand = [f for f in self.fm if t[-1] in f[2] and f[2][0][:3] == t[0][:3]]
            if len(t) == 1:  # solo nombre de pila: únicamente si es inequívoco
                res = cand[0][0] if len(cand) == 1 else None
            elif cand:
                prim = [f for f in cand if f[3] == PRIMARY_GROUP]
                if prim:
                    cand = prim
                if len(cand) > 1:
                    exact = [f for f in cand if f[2] == t]
                    if len(exact) == 1:
                        cand = exact
                if len(cand) > 1:
                    prefix = [f for f in cand if " ".join(f[2]).startswith(key + " ")]
                    if len(prefix) == 1:
                        cand = prefix
                if len(cand) == 1:
                    res = cand[0][0]
        self.cache[key] = res
        return res

    def ids_for(self, text):
        out = []
        for part in (text or "").split("/"):
            part = part.strip()
            if not part:
                continue
            rid = self.resolve(part)
            if rid:
                if rid not in out:
                    out.append(rid)
            else:
                self.unmatched[part] += 1
        return out


WORKERS = 4
POOL = ThreadPoolExecutor(WORKERS)


def send_batch(gql, board_id, rel_col, batch):
    """batch: [(item_id, [ids])]. Con [] vacía la relación."""
    parts = []
    for n, (iid, ids) in enumerate(batch):
        val = json.dumps(json.dumps({rel_col: {"item_ids": ids}}))
        parts.append("m%d: change_multiple_column_values(board_id:%s,item_id:%s,column_values:%s){id}"
                     % (n, board_id, iid, val))
    gql("mutation{" + " ".join(parts) + "}")
    return len(batch)


def backfill(gql, resolver, board_id, cfg, deadline, per_minute, batch_size):
    fields = 'cursor items{id column_values(ids:["%s"]){text}}' % cfg["text"]
    first = ('query($b:ID!,$g:CompareValue!){boards(ids:[$b]){items_page(limit:500,query_params:{rules:['
             '{column_id:"group",compare_value:$g,operator:any_of},'
             '{column_id:"%s",compare_value:"",operator:is_not_empty},'
             '{column_id:"%s",compare_value:"",operator:is_empty}]}){%s}}}') % (cfg["text"], cfg["rel"], fields)
    nxt = 'query($c:String!){next_items_page(limit:500,cursor:$c){%s}}' % fields
    page = gql(first, {"b": str(board_id), "g": cfg["groups"]})["boards"][0]["items_page"]
    updated = skipped = 0
    interval = 60.0 * batch_size / per_minute
    while True:
        work = []
        for it in page["items"]:
            ids = resolver.ids_for(it["column_values"][0]["text"])
            if ids:
                work.append((it["id"], ids))
            else:
                skipped += 1
        # Cada lote de 20 tarda 10-20 s en responder, así que se envían varios a la vez
        # (WORKERS) y se espacia el inicio de cada lote para no pasar de --per-minute.
        futures = []
        for i in range(0, len(work), batch_size):
            if time.time() > deadline:
                break
            futures.append(POOL.submit(send_batch, gql, board_id, cfg["rel"], work[i:i + batch_size]))
            time.sleep(interval)
        updated += sum(f.result() for f in futures)
        if time.time() > deadline:
            return updated, skipped, False
        print(f"  [{cfg['name']}] actualizados={updated} sin_match={skipped}", flush=True)
        if not page["cursor"] or time.time() > deadline:
            return updated, skipped, not page["cursor"]
        page = gql(nxt, {"c": page["cursor"]})["next_items_page"]


def clear(gql, board_id, cfg, deadline, per_minute, batch_size):
    """Vacía la relación en los grupos de cfg (solo filas que la tienen). Reanudable:
    cada ejecución vuelve a buscar las que aún tienen algo enlazado."""
    fields = "cursor items{id}"
    first = ('query($b:ID!,$g:CompareValue!){boards(ids:[$b]){items_page(limit:500,query_params:{rules:['
             '{column_id:"group",compare_value:$g,operator:any_of},'
             '{column_id:"%s",compare_value:"",operator:is_not_empty}]}){%s}}}') % (cfg["rel"], fields)
    nxt = 'query($c:String!){next_items_page(limit:500,cursor:$c){%s}}' % fields
    page = gql(first, {"b": str(board_id), "g": cfg["groups"]})["boards"][0]["items_page"]
    cleared = 0
    interval = 60.0 * batch_size / per_minute
    while True:
        work = [(it["id"], []) for it in page["items"]]
        futures = []
        for i in range(0, len(work), batch_size):
            if time.time() > deadline:
                break
            futures.append(POOL.submit(send_batch, gql, board_id, cfg["rel"], work[i:i + batch_size]))
            time.sleep(interval)
        cleared += sum(f.result() for f in futures)
        print(f"  [{cfg['name']}] vaciados={cleared}", flush=True)
        if time.time() > deadline:
            return cleared, False
        if not page["cursor"]:
            return cleared, True
        page = gql(nxt, {"c": page["cursor"]})["next_items_page"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=340)
    ap.add_argument("--per-minute", type=int, default=200,
                    help="Elementos por minuto (cada uno cuesta 30.000 de complejidad; 200/min ≈ 6M/min)")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--boards", default=",".join(str(b) for b in BOARDS))
    ap.add_argument("--clear", action="store_true",
                    help="Vaciar 'Formador (rel)' en vez de rellenarla (exige --boards explícito)")
    args = ap.parse_args()
    if args.clear and "--boards" not in " ".join(sys.argv):
        sys.exit("Con --clear hay que indicar --boards (p. ej. --boards 5094417029)")

    token = os.environ.get("MONDAY_TOKEN")
    if not token:
        sys.exit("Falta MONDAY_TOKEN")
    gql = make_gql(token)
    deadline = time.time() + args.max_minutes * 60
    if args.clear:
        all_done = True
        for b in [int(x) for x in args.boards.split(",") if x]:
            cfg = BOARDS[b]
            print(f"Vaciando Formador (rel) · {cfg['name']} ({b}) · grupos {cfg['groups']}", flush=True)
            n, done = clear(gql, b, cfg, deadline, args.per_minute, args.batch_size)
            print(f"  -> vaciados={n} {'COMPLETO' if done else 'PENDIENTE'}", flush=True)
            all_done &= done
        print("TODO COMPLETO" if all_done else "Quedan elementos: vuelve a lanzar el workflow")
        return
    resolver = FormadorResolver(gql)
    all_done = True
    for b in [int(x) for x in args.boards.split(",") if x]:
        cfg = BOARDS[b]
        print(f"Tablero {cfg['name']} ({b})", flush=True)
        up, sk, done = backfill(gql, resolver, b, cfg, deadline, args.per_minute, args.batch_size)
        print(f"  -> actualizados={up} sin_match={sk} {'COMPLETO' if done else 'PENDIENTE'}", flush=True)
        all_done &= done
        if time.time() > deadline:
            all_done = False
            break
    print("Nombres sin coincidencia en Formadores:")
    for name, n in resolver.unmatched.most_common():
        print(f"  {name}: {n}")
    print("TODO COMPLETO" if all_done else "Quedan elementos: vuelve a lanzar el workflow")


if __name__ == "__main__":
    main()
