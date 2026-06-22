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

## Cambiar la hora

Edita el `cron` en `sync-encuestas.yml` (UTC; España = UTC+1 invierno / +2 verano).

## Lanzar a mano y verificar

GitHub → Actions → "Sync encuestas…" → **Run workflow**. En el log debe verse
que se procesan cursos de AMBOS tipos (no solo "Executive Global Hospitality")
y terminar con `Errores: 0`. El dashboard se refresca en 1–2 min.
