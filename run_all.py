"""
run_all.py — ejecuta el sync completo de encuestas y termina (Cloud Run Job).

A diferencia de server.py (servicio HTTP), aquí NO se escucha en ningún puerto:
el contenedor corre estos pasos en orden y sale. Es lo que dispara Cloud Scheduler
una vez al día. Las credenciales se leen de variables de entorno del Job
(MONDAY_TOKEN, EVOLCAMPUS_KEY, EVOLCAMPUS_CLIENT_ID).
"""
import os
import sys
import subprocess
import urllib.request

# Igual que el workflow de GitHub: por defecto SOLO EGH + Programas (Cursos). El board de
# Subvenciones (5100940645) se incluye solo si SYNC_SUBVENCIONES=1 (env). Así, cada noche se
# actualizan los alumnos actuales y las subvenciones se lanzan cuando estén preparadas.
_SUBV = os.environ.get("SYNC_SUBVENCIONES", "") in ("1", "true", "yes")

STEPS = [
    (["python", "scripts/evolcampus_monday_sync_cursos.py", "--sync"], {"SKIP_DASHBOARD_REGEN": "1"}),
]
if _SUBV:
    # 1b · Subvenciones RSK/AEHCOS → board 5100940645 (solo grupos RSK/AEHCOS).
    STEPS.append((["python", "scripts/evolcampus_monday_sync_cursos.py", "--sync", "--subvenciones"], {"SKIP_DASHBOARD_REGEN": "1"}))
STEPS += [
    (["python", "scripts/evolcampus_monday_sync.py", "--sync"], {}),
    # 2b · backfill automático LGTBI
    (["python", "scripts/evolcampus_monday_sync_cursos.py", "--sync", "--force", "--filter-course", "LGTBI"], {"SKIP_DASHBOARD_REGEN": "1"}),
    (["python", "scripts/link_encuestas_alumnos.py", "--board", "cursos"], {}),
    # Vincula cada encuesta con su MATRÍCULA por DNI + Grupo EvolCampus (rellena los
    # mirrors Cuenta e ID_Evolcampus). Alcance actual: Cursos + EGH.
    (["python", "scripts/link_matriculas_encuestas.py", "--board", "ambos"], {}),
]
if _SUBV:
    # 3b · Enlazar Alumno (rel) + Empresa en el board Subvenciones (clon de Cursos).
    STEPS.append((["python", "scripts/link_encuestas_alumnos.py", "--board", "subvenciones"], {}))
if _SUBV:
    STEPS.append((["python", "scripts/link_marca_matriculas.py"], {}))
    STEPS.append((["python", "scripts/sync_mirror_empresa.py"], {}))
else:
    # Solo EGH + Cursos (no tocar el board de subvenciones).
    STEPS.append((["python", "scripts/link_marca_matriculas.py", "--board", "egh"], {}))
    STEPS.append((["python", "scripts/link_marca_matriculas.py", "--board", "cursos"], {}))
    STEPS.append((["python", "scripts/sync_mirror_empresa.py", "--board", "egh"], {}))
    STEPS.append((["python", "scripts/sync_mirror_empresa.py", "--board", "cursos"], {}))

REFRESH_URL = ("https://rrhh.schoolers.io/.netlify/functions/monday-encuestas-build-background"
               + ("" if _SUBV else "?skip=subvenciones"))


def main():
    failed = []
    for cmd, extra in STEPS:
        env = {**os.environ, **extra}
        print(">> " + " ".join(cmd), flush=True)
        r = subprocess.run(cmd, env=env)
        if r.returncode != 0:
            failed.append(" ".join(cmd))
            print(f"   ⚠️  terminó con código {r.returncode} (se continúa)", flush=True)

    # Paso 6 · refrescar el dashboard (siempre, aunque algún paso haya fallado)
    try:
        urllib.request.urlopen(urllib.request.Request(REFRESH_URL, method="POST"), timeout=30)
        print(">> refresco Netlify OK", flush=True)
    except Exception as e:
        print(f"   ⚠️  refresco Netlify: {e}", flush=True)

    if failed:
        print("FALLARON pasos: " + " | ".join(failed), flush=True)
        sys.exit(1)   # marca la ejecución como fallida en Cloud Run (visible en el historial)
    print("SYNC OK", flush=True)


if __name__ == "__main__":
    main()
