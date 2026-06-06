# AGENTS.md — foscam-controller

Guía mínima para agentes de código. Detalle de usuario en [README.md](README.md).

## Stack

- **Python** ≥3.7, empaquetado con **setuptools** (`pyproject.toml`)
- **CLI**: `argparse` → entry point `foscam` (`foscam/cli.py`)
- **HTTP/CGI**: `requests` (`foscam/client.py`)
- **Visor**: `customtkinter` + `opencv-python` + `Pillow` + hilos (`foscam/viewer.py`)
- **Audio/stream**: `av` (PyAV), `sounddevice`; opcional `ffplay` (FFmpeg)
- **Motion/overlays**: `numpy` + OpenCV (`foscam/motion.py`)
- **GPU decode** (opcional): OpenCV+GStreamer, `nvh264dec` (NVIDIA)
- **Tests**: `pytest` (dev extra: `pip install -e ".[dev]"`)

## Comandos importantes

| Acción | Comando |
|--------|---------|
| Instalar (dev) | `pip install -e ".[dev]"` |
| Tests (CI) | `pytest tests/ -m "not integration"` |
| Tests integración | `pytest tests/ -m integration` (requiere `ffplay`/binarios) |
| Test unitario | `pytest tests/test_viewer_display.py -q` |
| CLI ayuda | `foscam --help` |
| Descubrir | `foscam discover [--network 192.168.1.0/24]` |
| Config/export | `foscam config --ip IP --user U --password P [--save cam.json]` |
| Aplicar config | `foscam apply --ip IP --user U --password P --file cam.json [--dry-run]` |
| Visor | `foscam view --ip IP --user U --password P` |
| Consola CGI | `foscam console --ip IP --user U --password P` |
| Sin entry point | `python -m foscam <subcomando>` |
| Publicar PyPI | `./scripts/publish_to_pypi.sh` |

**No detectado**: lint, typecheck, formatter, Makefile, Docker, migrate.

## Estructura

```
foscam/
  cli.py           # subcomandos discover|config|apply|view|console
  client.py        # FoscamClient (CGI HTTP)
  discover.py      # escaneo red
  config_io.py     # backup/restore JSON de cámara
  viewer.py        # visor RTSP/PTZ/audio (archivo grande)
  display_pacing.py # lógica pura de pacing/decimación (testeable)
  motion.py        # detección, overlays, auto-zoom, settings
  audio_gate.py    # puerta de ruido en vivo
  cgi_console.py   # REPL comandos CGI
  ui/              # shell CustomTkinter (theme, widgets, overlay)
tests/             # pytest; marker `integration` para ffplay
scripts/           # utilidades (publish, probes)
.github/workflows/ # CI: pytest sin integration
```

Prefs del visor: `~/.config/foscam-controller/viewer.json`.

## Convenciones

- **Idioma**: README y mensajes CLI en español; commits recientes en inglés o español (imperativo breve).
- **Cambios mínimos**: `viewer.py` es monolítico; tocar solo lo necesario; extraer lógica pura a módulos pequeños (patrón `display_pacing.py`).
- **Settings**: dataclasses en `motion.py`; persistencia JSON en prefs del visor.
- **Errores CGI**: `FoscamClient.send()` devuelve XML texto o `None`; no asumir dict parseado.
- **UI**: tema oscuro vía `foscam/ui/theme.py`; assets en `foscam/ui/assets/`.
- **Scope**: no mezclar refactors de UI con cambios de red/CGI en el mismo diff.

## Tests

- Ubicación: `tests/test_*.py`
- CI corre solo unitarios (`not integration`)
- Integración audio: `test_audio_gate_integration.py` (necesita `ffplay`)
- Lógica de pacing: `tests/test_viewer_display.py` — preferir tests de funciones puras
- Cobertura mínima explícita: no detectada; añadir tests solo si cubren comportamiento real

## Qué NO hacer

- No commitear JSON con credenciales ni IPs de producción
- No ampliar scope (refactors masivos, reformatear todo, nuevas deps sin pedir)
- No tocar APIs públicas (`FoscamClient`, CLI flags) sin motivo claro
- No asumir que `foscam discover` encuentra cámaras con auth (muchas requieren credenciales)
- No hardcodear contraseñas en código ni tests
- No reescribir `viewer.py` entero para optimizar; cambios incrementales
- No duplicar el README en respuestas ni en este archivo

## Eficiencia de contexto

- Leer primero el módulo afectado, no todo `viewer.py`
- Regla Cursor opcional: `.cursor/rules/ahorro.mdc`

## Learned Patterns

- Lógica testeable del visor → funciones puras en módulo aparte (`display_pacing.py`)
- Hilos del visor: lector RTSP, prep display, Tk solo pinta
- GPU NVIDIA: auto si `nvh264dec` disponible; vídeo OpenCV, audio sidecar PyAV
