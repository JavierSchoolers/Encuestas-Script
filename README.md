# Sync EvolCampus → Monday (encuestas)

Paquete listo para subir al repo PRIVADO de GitHub que sincroniza las encuestas
de EvolCampus a Monday, en los **dos** boards que lee el dashboard.

## Qué hace

El workflow `.github/workflows/sync-encuestas.yml` ejecuta, todos los días a las
00:30 hora de España (y a mano con "Run workflow"):

1. **`evolcampus_monday_sync_cursos.py --sync`** → board **Cursos `5094417029`**
   (todos los cursos NO-EGH: Housekeeping, Ofimática, etc.).
2. **`evolcampus_monday_sync.py --sync`** → board **EGH `5093144633`**
   (cursos EGH / Executive Global Hospitality / Gestión Responsable).
   Al terminar lanza `link_encuestas_alumnos.py`, que enlaza "empresa" y
   "alumno" por DNI en **ambos** boards (por eso Cursos va primero: así sus
   ítems ya existen cuando corre el enlace).
3. **`link_marca_matriculas.py`** → rellena "Empresa - dashboard" en ambos boards
   con la Marca real del alumno (cadena Matrículas FUNDAE → Sociedades → Cuentas).
4. **`sync_mirror_empresa.py`** → copia la empresa del board Alumnos (vía la
   relación que dejó el paso 2) a la columna de texto "Cuenta empresa".
5. **POST** a Netlify para reconstruir el JSON de encuestas del dashboard
   (`monday-encuestas-build-background`), que lee ambos boards y los combina.

> Antes solo se ejecutaba el script EGH, así que el board de Cursos quedaba
> obsoleto y los cursos no-EGH (p. ej. Housekeeping) no se actualizaban.

## Credenciales (GitHub Secrets)

Configúralas en **Settings → Secrets and variables → Actions**:

- `MONDAY_TOKEN`
- `EVOLCAMPUS_KEY`
- `EVOLCAMPUS_CLIENT_ID`

Los scripts las leen de variables de entorno y **abortan con error claro** si
falta alguna. **No hay credenciales en el código.**

> ⚠️ El token de Monday estuvo en texto plano en versiones anteriores. Conviene
> **rotarlo** en Monday y actualizar el Secret.

## Estructura

```
.github/workflows/sync-encuestas.yml
requirements.txt                 (requests)
scripts/
  evolcampus_monday_sync.py        (EGH;  --sync lanza link_encuestas_alumnos.py)
  evolcampus_monday_sync_cursos.py (NO-EGH; en CI salta monday_to_dashboard.py)
  link_encuestas_alumnos.py        (enlaza empresa/alumno por DNI en ambos boards)
  link_marca_matriculas.py         (empresa por Marca real desde Matrículas FUNDAE)
  sync_mirror_empresa.py           (espejo empresa Alumnos → texto "Cuenta empresa")
  monday_config.json               (board EGH 5093144633 + col_ids)
  monday_config_cursos.json        (board Cursos 5094417029 + col_ids)
tools/
  gen_egh_parte_map.py             (regenera el mapa tema→Parte EGH desde el Excel)
```

## Disparo diario (cron externo)

NO usamos el cron interno de GitHub (es "best-effort": se saltaba y desplazaba
ejecuciones). El workflow se lanza con el evento `workflow_dispatch` desde un
programador externo (cron-job.org) a las **00:30 hora de España**.

**1) Token de GitHub (una vez):**
- GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate.
- Repository access: **Only select repositories → Encuestas-Script**.
- Permissions → Repository → **Actions: Read and write**.
- Copia el token (empieza por `github_pat_…`). Guárdalo en secreto.

**2) cron-job.org (una vez):**
- Crea una cuenta gratis → **Create cronjob**.
- URL: `https://api.github.com/repos/JavierSchoolers/Encuestas-Script/actions/workflows/sync-encuestas.yml/dispatches`
- Método: **POST**
- Headers:
  - `Authorization: Bearer github_pat_…`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- Body: `{"ref":"main"}`
- Schedule: **00:30**, todos los días, timezone **Europe/Madrid** (cron-job.org sí
  ajusta el horario de verano → 00:30 reales todo el año).

**Probar el disparo a mano (terminal):**
```bash
curl -X POST \
  -H "Authorization: Bearer github_pat_…" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/JavierSchoolers/Encuestas-Script/actions/workflows/sync-encuestas.yml/dispatches \
  -d '{"ref":"main"}'
```
Respuesta `204 No Content` = OK; en Actions aparece un run nuevo en segundos.

## Lanzar a mano y verificar

GitHub → Actions → "Sync encuestas…" → **Run workflow**. En el log debe verse
que se procesan cursos de AMBOS tipos (no solo "Executive Global Hospitality")
y terminar con `Errores: 0`. El dashboard se refresca en 1–2 min.
