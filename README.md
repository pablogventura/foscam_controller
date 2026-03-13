# Foscam Controller

Herramientas unificadas para gestionar cámaras Foscam en la red: descubrimiento, configuración, visor en vivo y consola CGI. Compatible con la API CGI de la mayoría de modelos (FI98xx, C1, R2, etc.).

**Paquete en PyPI:** [foscam-controller](https://pypi.org/project/foscam-controller/)

---

## Instalación

### Desde PyPI (recomendado)

```bash
pip install foscam-controller
```

Para tener el comando `foscam` disponible globalmente sin afectar otros proyectos:

```bash
pipx install foscam-controller
```

### Desde el código fuente

Si clonas el repositorio:

```bash
cd foscam_controller
pip install -e .
# o con pipx:
pipx install .
```

Para actualizar o desinstalar:

```bash
pip upgrade foscam-controller
# pipx:
pipx upgrade foscam-controller
pipx uninstall foscam-controller
```

---

## Características

- **Descubrimiento**: Escaneo de la red local para encontrar cámaras Foscam (puertos 80, 88, 8080).
- **Configuración**: Exportar toda la configuración de una cámara a JSON y aplicarla a otras (con modo dry-run).
- **Visor**: GUI para ver el stream RTSP en vivo, control PTZ con flechas, snapshot, audio (PyAV o ffplay).
- **Consola CGI**: Terminal con autocompletado (Tab) para probar cualquier comando de la API.

---

## Requisitos

- Python 3.7+
- Red local con cámaras Foscam (misma subred)

Opcional para el visor: **audio** → `av`, `sounddevice` y/o `ffplay` (FFmpeg). **Decodificación GPU** → OpenCV con GStreamer y plugins nvcodec (NVIDIA).

---

## Uso

El comando principal es **`foscam`**. Ver ayuda:

```bash
foscam --help
foscam discover --help
```

### Descubrir cámaras

```bash
foscam discover
foscam discover --network 192.168.1.0/24 --workers 100
```

### Obtener configuración

```bash
foscam config --ip 192.168.1.6 --user admin --password TU_PASSWORD [--port 88] [--save cam.json]
```

### Aplicar configuración desde JSON

```bash
# Vista previa (recomendado primero)
foscam apply --ip 192.168.1.7 --user admin --password xxx --file cam.json --dry-run
# Aplicar
foscam apply --ip 192.168.1.7 --user admin --password xxx --file cam.json
```

### Visor en vivo

```bash
foscam view --ip 192.168.1.6 --user admin --password xxx [--port 88] [--sub] [--nvidia]
```

Atajos en el visor:

| Tecla      | Acción                    |
|-----------|----------------------------|
| Flechas   | Mover PTZ (mantener/soltar)|
| 0         | Ir al preset por defecto   |
| a / z     | Subir / bajar volumen      |
| Snapshot  | Guardar captura            |

Ejecutar el visor como módulo (sin usar el CLI):

```bash
python -m foscam.viewer --ip 192.168.1.6 --user admin --password xxx [--sub] [--audio-gate-db -38] [--nvidia]
```

### Consola de comandos CGI

```bash
foscam console --ip 192.168.1.6 --user admin --password xxx [--port 88]
```

Dentro: `help`, `list`, `doc <comando>`, y cualquier comando CGI con parámetros `clave=valor`. **Tab** para autocompletar.

### Sin instalar el comando global

Desde el directorio del proyecto:

```bash
python -m foscam discover
python -m foscam view --ip 192.168.1.6 --user admin --password xxx
```

---

## Estructura del proyecto

```
foscam_controller/
├── pyproject.toml          # Metadatos, dependencias y entry point
├── MANIFEST.in             # Archivos incluidos en el paquete fuente
├── requirements.txt        # Dependencias (referencia)
├── README.md
├── foscam/
│   ├── __init__.py
│   ├── __main__.py         # python -m foscam
│   ├── cli.py              # CLI (discover, config, apply, view, console)
│   ├── client.py           # Cliente HTTP API CGI (FoscamClient)
│   ├── discover.py         # Descubrimiento en red
│   ├── config_io.py        # Lectura/escritura de configuración JSON
│   ├── viewer.py           # Visor GUI (RTSP, PTZ, snapshot, audio)
│   └── cgi_console.py      # Consola interactiva CGI
└── scripts/
    └── publish_to_pypi.sh  # Publicar en PyPI (--test para Test PyPI)
```

---

## Uso como biblioteca

```python
from foscam.client import FoscamClient
from foscam.discover import FoscamDiscoverer
from foscam.config_io import FoscamConfigReader, FoscamConfigWriter

# Cliente para una cámara
client = FoscamClient("192.168.1.6", "admin", "password", port=88)
print(client.get_dev_name())
client.ptz_move("Up")
client.ptz_stop()

# Descubrir cámaras
disc = FoscamDiscoverer("192.168.1.0/24")
cameras = disc.discover(show_progress=True)

# Leer y guardar configuración
reader = FoscamConfigReader(client)
config = reader.get_all_config(show_progress=True)
reader.save_to_file("backup.json", config)

# Aplicar configuración
writer = FoscamConfigWriter(client)
writer.apply_from_file("backup.json", dry_run=False)
```

---

## Seguridad

- No subas archivos JSON de configuración que contengan contraseñas.
- Usa `--dry-run` antes de aplicar configuración en producción.

---

## Publicar en PyPI

1. Instalar: `pip install build twine`
2. Configurar token de PyPI: `TWINE_USERNAME=__token__` y `TWINE_PASSWORD=pypi-xxx` (crear en [pypi.org/manage/account/token](https://pypi.org/manage/account/token/))
3. Ejecutar:
   ```bash
   ./scripts/publish_to_pypi.sh          # PyPI
   ./scripts/publish_to_pypi.sh --test    # Test PyPI
   ```

---

## Referencias

- API CGI Foscam: documentación oficial del fabricante (Foscam IPCamera CGI User Guide).
