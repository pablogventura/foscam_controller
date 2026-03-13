#!/usr/bin/env python3
"""
Foscam Camera Viewer with GUI
Displays live video stream from Foscam cameras.
Takes connection parameters from command line arguments.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import cv2
from PIL import Image, ImageTk
import threading
import time
import queue
import argparse
import os
import re
import signal
import sys
import subprocess
import shutil
import tempfile
import requests
from urllib.parse import urlencode, quote
import xml.etree.ElementTree as ET

from foscam.client import FoscamClient

# Audio: PyAV + sounddevice (un solo demux, sync vídeo/audio) o ffplay como fallback
try:
    import av
    import sounddevice as sd
    import numpy as np
    _PYAV_OK = True
except (ImportError, OSError):
    # OSError: sounddevice no encuentra libportaudio (ej. instalar libportaudio2)
    _PYAV_OK = False
    sd = None
    np = None
PYAV_AUDIO_AVAILABLE = _PYAV_OK
AUDIO_AVAILABLE = PYAV_AUDIO_AVAILABLE or (shutil.which("ffplay") is not None)


class FoscamViewer:
    """GUI application to view Foscam camera streams."""
    
    def __init__(self, root, ip, port, user, password, use_sub_stream=False, audio_gate_db=None, use_nvidia_decode=False):
        self.root = root
        self.camera_name = None  # Fetched from camera after connection
        self.root.title(f"Foscam Camera Viewer - {ip}:{port}")
        self.root.geometry("1024x768")
        
        # Camera connection parameters (from command line)
        self.camera_ip = ip
        self.camera_user = user
        self.camera_password = password
        self.camera_port = port
        self._cgi_client = FoscamClient(ip, user, password, port)
        self.use_sub_stream = use_sub_stream  # Prefer videoSub (lower resolution)
        # Audio: solo pasar sonido por encima de este umbral en dB (ej. -40). None = sin puerta
        self.audio_gate_db = audio_gate_db
        # Decodificación por GPU NVIDIA (GStreamer nvh264dec) si está disponible
        self.use_nvidia_decode = use_nvidia_decode
        
        # Video stream
        self.cap = None
        self.is_streaming = False
        self.rtsp_url = None  # Set when video connects, used for audio
        self.frame_queue = queue.Queue(maxsize=2)  # Small queue to keep frames fresh
        self.video_thread = None
        self.audio_thread = None
        self.current_frame = None
        # Modo PyAV: un solo contenedor para vídeo+audio (sync). None si usamos OpenCV+ffplay
        self._av_container = None
        self._av_demux_thread = None
        self._audio_queue = None   # queue de arrays float32 para sounddevice (modo PyAV)
        self._sd_stream = None     # sounddevice.OutputStream (modo PyAV)
        self._use_pyav = False     # True si estamos en ruta PyAV (vídeo+audio desde av.open)
        # Audio ffplay (solo cuando PyAV no está en uso)
        self._audio_process = None
        self._audio_stop = threading.Event()
        # PTZ: current direction (to ignore key repeat) y cola para envío sin crear hilo por tecla
        self._ptz_direction = None
        self._ptz_queue = queue.Queue()
        self._ptz_worker_thread = threading.Thread(target=self._ptz_worker, daemon=True)
        self._ptz_worker_thread.start()
        # Display size (updated on Configure; used to scale video to window)
        self._display_size = None
        # Volumen de reproducción (ffplay), 0–100; a/z lo modifican y reinician ffplay
        self._playback_volume = 50
        self._audio_restart_in_progress = False  # evita varios reinicios simultáneos (varios procesos ffplay)
        # Create GUI
        self._create_widgets()
        
        # Bind close event
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # PTZ: bind arrow keys (move on press, stop on release)
        self._bind_ptz_keys()
        
        # Start connection automatically
        self._connect_camera()
    
    def _build_params_display(self):
        """Texto con todos los parámetros de consola (contraseña enmascarada)."""
        parts = [
            f"--ip {self.camera_ip}",
            f"--port {self.camera_port}",
            f"--user {self.camera_user}",
            "--password ***",
        ]
        if self.use_sub_stream:
            parts.append("--sub")
        if self.audio_gate_db is not None:
            parts.append(f"--audio-gate-db {self.audio_gate_db}")
        if self.use_nvidia_decode:
            parts.append("--nvidia")
        return "  |  ".join(parts)
    
    def _create_widgets(self):
        """Create GUI widgets."""
        # Status bar first so it stays fixed at bottom (pack order matters)
        status_frame = ttk.Frame(self.root)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value=f"Connecting to {self.camera_ip}:{self.camera_port}...")
        self.resolution_var = tk.StringVar(value="")
        self.display_size_var = tk.StringVar(value="")
        self.decode_backend_var = tk.StringVar(value="")  # "NVIDIA" o "" cuando hay decodificación por GPU
        self.volume_var = tk.StringVar(value="Vol: --")  # Volumen de audio de la cámara (a/z)
        res_label = ttk.Label(status_frame, textvariable=self.resolution_var, relief=tk.SUNKEN, anchor=tk.E, width=12)
        res_label.pack(side=tk.RIGHT, padx=(4, 0))
        disp_label = ttk.Label(status_frame, textvariable=self.display_size_var, relief=tk.SUNKEN, anchor=tk.E, width=14)
        disp_label.pack(side=tk.RIGHT, padx=(4, 0))
        decode_label = ttk.Label(status_frame, textvariable=self.decode_backend_var, relief=tk.SUNKEN, anchor=tk.E, width=9)
        decode_label.pack(side=tk.RIGHT, padx=(4, 0))
        vol_label = ttk.Label(status_frame, textvariable=self.volume_var, relief=tk.SUNKEN, anchor=tk.E, width=8)
        vol_label.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Top frame for controls
        control_frame = ttk.Frame(self.root, padding="10")
        control_frame.pack(side=tk.TOP, fill=tk.X)
        
        # Fila 1: camera info + botones
        row1 = ttk.Frame(control_frame)
        row1.pack(side=tk.TOP, fill=tk.X)
        self.info_var = tk.StringVar(value=f"Camera: {self.camera_ip}:{self.camera_port} | User: {self.camera_user}")
        ttk.Label(row1, textvariable=self.info_var).pack(side=tk.LEFT, padx=5)
        self.disconnect_btn = ttk.Button(row1, text="Disconnect", command=self._disconnect_camera)
        self.disconnect_btn.pack(side=tk.RIGHT, padx=5)
        self.snapshot_btn = ttk.Button(row1, text="Snapshot", command=self._take_snapshot)
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)
        
        # Fila 2: todos los parámetros de consola (contraseña enmascarada)
        self._params_var = tk.StringVar(value=self._build_params_display())
        params_label = ttk.Label(control_frame, textvariable=self._params_var, font=("TkDefaultFont", 8))
        params_label.pack(side=tk.TOP, anchor=tk.W, padx=5, pady=(2, 0))
        
        # Video frame (fills space between control and status bar)
        video_frame = ttk.Frame(self.root, padding="10")
        video_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        
        # Video label (fills window; display loop scales frame to this size)
        self.video_label = ttk.Label(video_frame, text="Connecting...", anchor=tk.CENTER)
        self.video_label.pack(fill=tk.BOTH, expand=True)
        # Redraw on resize so scaling updates immediately
        self.video_label.bind("<Configure>", self._on_display_resize)
        
        # Start frame update loop
        self._update_display_loop()
    
    def _bind_ptz_keys(self):
        """Bind arrow keys for PTZ control (move on press, stop on release). Tecla 0: ir al preset por defecto."""
        key_commands = {
            "Up": "ptzMoveUp",
            "Down": "ptzMoveDown",
            "Left": "ptzMoveLeft",
            "Right": "ptzMoveRight",
        }
        for key, cmd in key_commands.items():
            self.root.bind(f"<KeyPress-{key}>", lambda e, c=cmd: self._ptz_move(c))
            self.root.bind(f"<KeyRelease-{key}>", lambda e: self._ptz_stop())
        self.root.bind("<KeyPress>", self._on_any_keypress)
        # Ensure window receives key events
        self.root.focus_set()
        self.root.bind("<FocusIn>", lambda e: self.root.focus_set())
    
    def _ptz_worker(self):
        """Worker que envía comandos PTZ desde la cola; un solo hilo evita latencia de crear hilo por tecla."""
        while True:
            try:
                item = self._ptz_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            cmd, params = item
            try:
                self._cgi_client.send(cmd, params or {})
            except Exception:
                pass

    def _ptz_cgi(self, cmd, params=None):
        """Encola comando PTZ para el worker (envío inmediato, sin crear hilo nuevo)."""
        if not self.is_streaming:
            return
        try:
            self._ptz_queue.put_nowait((cmd, params))
        except queue.Full:
            pass
    
    def _ptz_move(self, cmd):
        """Start PTZ movement (arrow key pressed). Only one move command per press (ignore key repeat)."""
        if self._ptz_direction is not None:
            return  # Already moving in some direction, ignore repeat
        self._ptz_direction = cmd
        self._ptz_cgi(cmd)
    
    def _ptz_stop(self):
        """Stop PTZ movement (arrow key released)."""
        self._ptz_direction = None
        self._ptz_cgi("ptzStopRun")

    def _on_any_keypress(self, event):
        """Tecla 0: ptzReset. a: subir volumen, z: bajar volumen (reproducción ffplay)."""
        if event.keysym in ("0", "KP_0") or getattr(event, "keycode", None) == 96:
            self._ptz_reset()
        elif event.keysym in ("a", "A"):
            self._volume_change(1)
        elif event.keysym in ("z", "Z"):
            self._volume_change(-1)

    def _volume_change(self, delta):
        """Sube o baja el volumen de reproducción. PyAV: solo actualiza (callback aplica). ffplay: reinicia proceso."""
        new_vol = max(0, min(100, self._playback_volume + delta * 10))
        if new_vol == self._playback_volume:
            return
        self._playback_volume = new_vol
        self.volume_var.set(f"Vol: {new_vol}")
        if self._use_pyav:
            return  # sounddevice callback aplica _playback_volume en tiempo real
        if not AUDIO_AVAILABLE or not self.rtsp_url or not self.is_streaming:
            return
        if self._audio_restart_in_progress:
            return
        self._audio_restart_in_progress = True
        def do_restart():
            self._audio_stop.set()
            self._terminate_audio_process()
            if self.audio_thread and self.audio_thread.is_alive():
                self.audio_thread.join(timeout=2.5)
            self._audio_stop.clear()
            self._audio_restart_in_progress = False
            if not self.is_streaming:
                return
            self.audio_thread = threading.Thread(target=self._audio_playback_thread, daemon=True)
            self.audio_thread.start()
        threading.Thread(target=do_restart, daemon=True).start()

    def _ptz_reset(self):
        """Envía comando ptzReset (posición por defecto)."""
        self._ptz_cgi("ptzReset")
    
    def _connect_camera(self):
        """Connect to the camera and start streaming."""
        # Start connection in a separate thread
        threading.Thread(target=self._start_stream, daemon=True).start()
    
    def _gst_uri_safe(self, rtsp_url):
        """URI con percent-encoding total para el pipeline GStreamer: evita que : / ! rompan el parser (grammar.y)."""
        return quote(rtsp_url, safe="")

    def _open_cap_gstreamer_nvidia(self, rtsp_url):
        """Abre VideoCapture con pipeline GStreamer usando decodificador NVIDIA (nvh264dec). Devuelve None si no está disponible."""
        try:
            build_info = cv2.getBuildInformation() or ""
            if "GStreamer: NO" in build_info or "GStreamer:   NO" in build_info:
                return None
            backend = getattr(cv2, "CAP_GSTREAMER", 1800)
        except Exception:
            return None
        # URI percent-encoded para que el parser no vea : / ! (grammar.y)
        uri_safe = self._gst_uri_safe(rtsp_url)
        # Caps entre comillas para que la coma no rompa el parser
        pipelines = [
            (
                f'uridecodebin uri={uri_safe} ! queue ! videoconvert ! "video/x-raw,format=BGR" ! appsink drop=1 max-buffers=2'
            ),
            (
                f'rtspsrc location={uri_safe} protocols=tcp latency=0 ! '
                'rtph264depay ! h264parse ! nvh264dec ! capsfilter caps="video/x-raw,format=NV12" ! '
                'videoconvert ! "video/x-raw,format=BGR" ! appsink drop=1 max-buffers=2'
            ),
        ]
        for pipeline in pipelines:
            try:
                cap = cv2.VideoCapture(pipeline, backend)
                if cap.isOpened():
                    return cap
                if cap is not None:
                    cap.release()
            except Exception:
                pass
        # Capturar error real de GStreamer para mostrarlo al usuario
        self._gst_nvidia_error = self._get_gstreamer_error(uri_safe)
        return None

    def _get_gstreamer_error(self, uri_safe):
        """Ejecuta gst-launch-1.0 con el pipeline y devuelve la última línea de ERROR de stderr (o None)."""
        pipeline = (
            f'uridecodebin uri={uri_safe} ! queue ! videoconvert ! "video/x-raw,format=BGR" ! fakesink'
        )
        try:
            # Escribir pipeline a archivo para evitar límites/escapado del argumento (grammar.y)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".gst", delete=False) as f:
                f.write(pipeline)
                tmp = f.name
            try:
                r = subprocess.run(
                    ["sh", "-c", f'gst-launch-1.0 -e "$(cat "{tmp}")"'],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    env={**os.environ, "GST_DEBUG": "2"},
                )
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            err = (r.stderr or "") + (r.stdout or "")
            for line in reversed(err.splitlines()):
                line = line.strip()
                if "ERROR" in line or "error:" in line.lower() or "Error" in line:
                    # Quitar códigos ANSI y prefijos de GST_DEBUG
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                    clean = re.sub(r"^[0-9:.]+\s+\S+\s+", "", clean)
                    if len(clean) > 80:
                        clean = clean[:77] + "..."
                    return clean if clean else None
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass
        return None

    def _start_stream(self):
        """Start the video stream."""
        # RTSP uses the same port as HTTP on many Foscam cameras (e.g. 88)
        # videoMain = main stream (higher res), videoSub = sub stream (lower res)
        base = f"rtsp://{self.camera_user}:{self.camera_password}@{self.camera_ip}:{self.camera_port}"
        if self.use_sub_stream:
            rtsp_urls = [
                f"{base}/videoSub",
                f"{base}/videoMain",
                f"{base}/videoStream",
                f"{base}/live",
                f"{base}/h264",
            ]
        else:
            rtsp_urls = [
                f"{base}/videoMain",
                f"{base}/videoSub",
                f"{base}/videoStream",
                f"{base}/live",
                f"{base}/h264",
            ]
        
        # Opciones FFmpeg para menor latencia: TCP, sin buffer extra, análisis rápido del stream
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|"
            "analyzeduration;500000|probesize;500000"
        )

        # Comprobar una vez si nvh264dec está disponible (para el mensaje si falla --nvidia)
        if self.use_nvidia_decode:
            try:
                r = subprocess.run(
                    ["gst-inspect-1.0", "nvh264dec"],
                    capture_output=True, timeout=5,
                    env={**os.environ, "GST_INSPECT_NO_COLORS": "1"},
                )
                self._nvh264dec_available = r.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
                self._nvh264dec_available = False
        else:
            self._nvh264dec_available = False

        for rtsp_url in rtsp_urls:
            try:
                self.root.after(0, lambda url=rtsp_url: self.status_var.set(f"Trying RTSP: {url.split('@')[1]}..."))
                
                # Ruta PyAV: un solo contenedor vídeo+audio (sync), sin subproceso
                if PYAV_AUDIO_AVAILABLE and not self.use_nvidia_decode:
                    container = None
                    try:
                        opts = {"rtsp_flags": "prefer_tcp", "fflags": "nobuffer", "flags": "low_delay"}
                        container = av.open(rtsp_url, options=opts)
                        video_stream = next((s for s in container.streams if s.type == "video"), None)
                        if video_stream is None:
                            container.close()
                        else:
                            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
                            self._av_container = container
                            self._av_video_stream = video_stream
                            self._av_audio_stream = audio_stream
                            self.rtsp_url = rtsp_url
                            self.root.after(0, lambda: self.status_var.set(f"Connected via RTSP (PyAV): {rtsp_url.split('@')[1]}"))
                            self.is_streaming = True
                            self._use_pyav = True
                            self._decode_backend = "ffmpeg"
                            self.root.after(0, self._update_ui_connected)
                            self._fetch_and_set_camera_name()
                            self.root.after(0, lambda v=self._playback_volume: self.volume_var.set(f"Vol: {v}"))
                            if audio_stream is not None:
                                sr = int(audio_stream.sample_rate)
                                ch = audio_stream.layout.channels if hasattr(audio_stream.layout, "channels") else 1
                                self._sd_channels = ch
                                self._audio_queue = queue.Queue(maxsize=min(2048, max(32, sr // 10)))
                                self._sd_stream = sd.OutputStream(
                                    samplerate=sr, channels=ch, dtype="float32",
                                    blocksize=1024, callback=self._sd_audio_callback,
                                )
                                self._sd_stream.start()
                            self._av_demux_thread = threading.Thread(target=self._av_demux_thread, daemon=True)
                            self._av_demux_thread.start()
                            return
                    except Exception:
                        if container is not None:
                            try:
                                container.close()
                            except Exception:
                                pass
                        self._av_container = None
                        self._use_pyav = False
                
                # Ruta OpenCV (+ ffplay si hay audio y no PyAV)
                self._decode_backend = "ffmpeg"
                if self.use_nvidia_decode:
                    self.cap = self._open_cap_gstreamer_nvidia(rtsp_url)
                    if self.cap is not None and self.cap.isOpened():
                        self._decode_backend = "nvidia"
                else:
                    self.cap = None
                if self.cap is None or not self.cap.isOpened():
                    if self.cap is not None:
                        self.cap.release()
                        self.cap = None
                    # Fallback: FFmpeg (CPU)
                    self.cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                    if self.cap.isOpened():
                        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        self._decode_backend = "ffmpeg"
                
                # Try to open
                if self.cap is not None and self.cap.isOpened():
                    # Flush initial buffer (pocos grab para no disparar av_frame_get_buffer / OOM)
                    for _ in range(4):
                        self.cap.grab()
                    ret, test_frame = self.cap.read()
                    if ret and test_frame is not None:
                        self.rtsp_url = rtsp_url
                        self.root.after(0, lambda: self.status_var.set(f"Connected via RTSP: {rtsp_url.split('@')[1]}"))
                        self.is_streaming = True
                        self.root.after(0, self._update_ui_connected)
                        # Fetch camera name and update window title
                        self._fetch_and_set_camera_name()
                        # Start video thread
                        self.video_thread = threading.Thread(target=self._video_reader_thread, daemon=True)
                        self.video_thread.start()
                        # Volumen de reproducción: mostrar valor actual (ffplay)
                        if AUDIO_AVAILABLE:
                            self.root.after(0, lambda v=self._playback_volume: self.volume_var.set(f"Vol: {v}"))
                            self._audio_stop.clear()
                            self.audio_thread = threading.Thread(target=self._audio_playback_thread, daemon=True)
                            self.audio_thread.start()
                        return
                    else:
                        self.cap.release()
                        self.cap = None
                
            except Exception as e:
                if self.cap:
                    self.cap.release()
                    self.cap = None
                continue
        
        # If all RTSP URLs failed
        self.root.after(0, lambda: self._connection_failed("Could not connect via RTSP. Please check:\n- Port (88 is common for Foscam)\n- Username and password\n- Same URL as: ffplay rtsp://user:pass@ip:88/videoMain"))
    
    def _fetch_and_set_camera_name(self):
        """Obtiene el nombre de la cámara vía CGI getDevName y actualiza el título."""
        def do_fetch():
            try:
                resp = self._cgi_client.get_dev_name()
                if resp:
                    root_el = ET.fromstring(resp)
                    dev_name_el = root_el.find("devName")
                    if dev_name_el is not None and dev_name_el.text:
                        name = dev_name_el.text.strip()
                        if name:
                            self.camera_name = name
                            self.root.after(0, lambda n=name: self.root.title(f"Foscam - {n}"))
                            self.root.after(0, lambda n=name: self.info_var.set(f"{n} | {self.camera_ip}:{self.camera_port}"))
            except Exception:
                pass
        threading.Thread(target=do_fetch, daemon=True).start()
    
    def _sd_audio_callback(self, outdata, frames, time_info, status):
        """Callback de sounddevice: rellena outdata desde la cola de audio (modo PyAV). Volumen aplicado aquí."""
        if status:
            pass  # underrun/overflow se ignoran para no saturar logs
        q = getattr(self, "_audio_queue", None)
        if q is None:
            outdata.fill(0)
            return
        vol = max(0, min(100, getattr(self, "_playback_volume", 50))) / 100.0
        need = frames * (getattr(self, "_sd_channels", 1))
        out = outdata.ravel()
        filled = 0
        while filled < need and q is not None:
            try:
                chunk = q.get_nowait()
                if chunk is None:
                    break
                n = min(len(chunk), need - filled)
                out[filled:filled + n] = chunk[:n] * vol
                filled += n
                if n < len(chunk):
                    # devolver el resto a la cola (raro; mejor descartar o usar buffer)
                    break
            except queue.Empty:
                break
        if filled < need:
            out[filled:need] = 0.0
    
    def _av_demux_thread(self):
        """Lee vídeo y audio del contenedor PyAV; pone frames BGR en frame_queue y muestras en _audio_queue."""
        container = self._av_container
        video_stream = self._av_video_stream
        audio_stream = getattr(self, "_av_audio_stream", None)
        audio_queue = self._audio_queue
        gate_linear = None
        if self.audio_gate_db is not None:
            gate_linear = 10.0 ** (self.audio_gate_db / 20.0)
        try:
            for packet in container.demux(video_stream, *([] if audio_stream is None else [audio_stream])):
                if not self.is_streaming:
                    break
                if packet.stream == video_stream:
                    for frame in packet.decode():
                        if not self.is_streaming:
                            break
                        try:
                            img = frame.reformat(format="bgr24")
                            arr = img.to_ndarray()
                            try:
                                self.frame_queue.put_nowait(arr)
                            except queue.Full:
                                try:
                                    self.frame_queue.get_nowait()
                                    self.frame_queue.put_nowait(arr)
                                except queue.Empty:
                                    pass
                        except Exception:
                            pass
                elif packet.stream == audio_stream and audio_queue is not None:
                    for frame in packet.decode():
                        if not self.is_streaming:
                            break
                        try:
                            arr = frame.to_ndarray()
                            if arr.dtype != np.float32:
                                arr = arr.astype(np.float32) / 32768.0
                            if gate_linear is not None:
                                arr = np.where(np.abs(arr) < gate_linear, 0.0, arr)
                            # Enviar por trozos si es muy grande (evitar bloques enormes)
                            ch = arr.shape[1] if arr.ndim > 1 else 1
                            step = 1024 * ch
                            for i in range(0, arr.size, step):
                                chunk = arr.ravel()[i:i + step].copy()
                                if len(chunk) > 0:
                                    try:
                                        audio_queue.put_nowait(chunk)
                                    except queue.Full:
                                        pass
                        except Exception:
                            pass
        except Exception as e:
            if self.is_streaming:
                self.root.after(0, lambda err=str(e): self.status_var.set(f"Stream: {err}"))
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self._av_container = None
    
    def _audio_playback_thread(self):
        """Play audio from RTSP using ffplay (no video window). -volume 0-100; opcional compand (puerta de ruido)."""
        if not AUDIO_AVAILABLE or not self.rtsp_url:
            return
        try:
            vol = max(0, min(100, getattr(self, "_playback_volume", 50)))
            cmd = ["ffplay", "-nodisp", "-autoexit", "-volume", str(vol), "-i", self.rtsp_url]
            # Puerta de volumen: silenciar por debajo de audio_gate_db (ej. -40 dB)
            if self.audio_gate_db is not None:
                th = self.audio_gate_db
                af = f"compand=attacks=0.05|0.05:decays=0.05|0.05:points=-90/-70|{th - 0.1}/-70|{th}/0"
                cmd = ["ffplay", "-nodisp", "-autoexit", "-volume", str(vol), "-af", af, "-i", self.rtsp_url]
            # start_new_session=True para poder matar el grupo al cerrar (evita que ffplay quede abierto)
            self._audio_process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            proc = self._audio_process  # referencia local: otro hilo puede poner _audio_process = None
            while proc is not None and proc.poll() is None and not self._audio_stop.is_set():
                time.sleep(0.2)
            if proc is not None and proc.poll() is None:
                self._terminate_audio_process()
        except FileNotFoundError:
            pass
        except Exception as e:
            if self.is_streaming:
                self.root.after(0, lambda err=str(e): self.status_var.set(f"Audio: {err}"))
        finally:
            self._audio_process = None
    
    def _video_reader_thread(self):
        """Thread to continuously read frames from RTSP stream."""
        # Limitar lectura a ~40 fps para no saturar av_frame_get_buffer (evitar OOM con FFmpeg)
        read_interval = 0.025
        while self.is_streaming and self.cap is not None:
            try:
                ret, frame = self.cap.read()
                if ret and frame is not None:
                    try:
                        self.frame_queue.put_nowait(frame)
                    except queue.Full:
                        try:
                            self.frame_queue.get_nowait()
                            self.frame_queue.put_nowait(frame)
                        except queue.Empty:
                            pass
                else:
                    # Stream ended or error
                    if self.is_streaming:
                        self.root.after(0, lambda: self.status_var.set("Stream interrupted, attempting reconnect..."))
                        time.sleep(2)
                        # Try to reconnect
                        if self.is_streaming:
                            self._start_stream()
                        break
                time.sleep(read_interval)
            except Exception as e:
                if self.is_streaming:
                    self.root.after(0, lambda err=str(e): self.status_var.set(f"Stream error: {err}"))
                time.sleep(1)
                if not self.is_streaming:
                    break
    
    def _on_display_resize(self, event):
        """When window/display area is resized, use new size for scaling only (no automatic resolution change)."""
        if event.widget != self.video_label or event.width < 2 or event.height < 2:
            return
        self._display_size = (event.width, event.height)

    def _update_display_loop(self):
        """Continuously update display from frame queue. Scales video to window size."""
        try:
            # Get latest frame from queue (non-blocking)
            try:
                frame = self.frame_queue.get_nowait()
                
                # Resize frame to fit display (dynamic: follows window size)
                if self._display_size and self._display_size[0] > 1 and self._display_size[1] > 1:
                    display_width, display_height = self._display_size
                else:
                    display_width = self.video_label.winfo_width()
                    display_height = self.video_label.winfo_height()
                
                if display_width > 1 and display_height > 1:
                    # Resize maintaining aspect ratio
                    height, width = frame.shape[:2]
                    scale = min(display_width / width, display_height / height)
                    new_width = int(width * scale)
                    new_height = int(height * scale)
                    
                    frame_resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
                else:
                    frame_resized = frame
                
                # Convert BGR to RGB
                frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                
                # Convert to PIL Image
                image = Image.fromarray(frame_rgb)
                photo = ImageTk.PhotoImage(image=image)
                
                # Update label
                self.video_label.config(image=photo, text="")
                self.video_label.image = photo  # Keep a reference
                
                # Store current frame for snapshot
                self.current_frame = frame
                # Show stream resolution in status bar (width x height)
                h, w = frame.shape[:2]
                self.resolution_var.set(f"{w}×{h}")
                # Show video area size used for display (and for sub stream CGI comparison)
                if display_width > 1 and display_height > 1:
                    self.display_size_var.set(f"Display {display_width}×{display_height}")
                
            except queue.Empty:
                # No new frame available, keep current display
                pass
                
        except Exception as e:
            if self.is_streaming:
                self.status_var.set(f"Display error: {str(e)}")
        
        # Schedule next update (aim for ~30 FPS display)
        # Refresco más frecuente (~50 FPS) para que el último frame se muestre antes
        self.root.after(20, self._update_display_loop)
    
    def _update_ui_connected(self):
        """Update UI after successful connection."""
        msg = f"Streaming from {self.camera_ip}:{self.camera_port}"
        if self.use_sub_stream:
            msg += " (sub stream)"
        if getattr(self, "_use_pyav", False):
            msg += " (PyAV, sync)"
        if AUDIO_AVAILABLE:
            msg += " (video + audio)"
        if self.use_nvidia_decode and getattr(self, "_decode_backend", "ffmpeg") != "nvidia":
            if getattr(self, "_nvh264dec_available", False):
                err = getattr(self, "_gst_nvidia_error", None)
                if err:
                    msg += f" (NVIDIA: {err})"
                else:
                    msg += " (NVIDIA: error pipeline)"
            else:
                msg += " (NVIDIA: instale plugin nvcodec, gst-inspect-1.0 nvh264dec)"
        msg += " | Flechas: PTZ, 0: ptzReset, a/z: volumen (reproducción)"
        self.status_var.set(msg)
        backend = getattr(self, "_decode_backend", "ffmpeg")
        if backend == "nvidia":
            self.decode_backend_var.set("NVIDIA")
        elif self.use_nvidia_decode:
            self.decode_backend_var.set("NVIDIA✗")
        else:
            self.decode_backend_var.set("")
    
    def _connection_failed(self, error_msg):
        """Handle connection failure."""
        self.status_var.set(f"Connection failed: {error_msg}")
        messagebox.showerror("Connection Error", f"Failed to connect to camera:\n{error_msg}")
        # Close window after showing error
        self.root.after(2000, self.root.destroy)
    
    def _terminate_audio_process(self):
        """Cierra el proceso de audio (ffplay); usa kill del grupo si hace falta."""
        if self._audio_process is None:
            return
        proc = self._audio_process
        self._audio_process = None
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                if hasattr(os, "killpg"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _disconnect_camera(self):
        """Disconnect from the camera and stop streaming."""
        self.is_streaming = False
        self.volume_var.set("Vol: --")
        if getattr(self, "_use_pyav", False):
            self._use_pyav = False
            self._audio_queue = None
            if self._sd_stream is not None:
                try:
                    self._sd_stream.stop()
                    self._sd_stream.close()
                except Exception:
                    pass
                self._sd_stream = None
            if self._av_demux_thread is not None and self._av_demux_thread.is_alive():
                self._av_demux_thread.join(timeout=2.0)
            self._av_demux_thread = None
            if self._av_container is not None:
                try:
                    self._av_container.close()
                except Exception:
                    pass
                self._av_container = None
        else:
            self._audio_stop.set()
            self._terminate_audio_process()
            if self.audio_thread and self.audio_thread.is_alive():
                time.sleep(0.3)
            if self.video_thread and self.video_thread.is_alive():
                time.sleep(0.5)
            if self.cap is not None:
                self.cap.release()
                self.cap = None
        
        # Clear frame queue
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break
        
        self.current_frame = None
        self.video_label.config(image="", text="Disconnected")
        self.video_label.image = None
        
        self.status_var.set("Disconnected")
        self.resolution_var.set("")
        self.display_size_var.set("")
    
    def _take_snapshot(self):
        """Take a snapshot and save it."""
        if not self.is_streaming or not hasattr(self, 'current_frame') or self.current_frame is None:
            messagebox.showwarning("Warning", "No active stream to capture")
            return
        
        try:
            # Generate filename with timestamp
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot_{self.camera_ip}_{timestamp}.jpg"
            
            # Save frame
            cv2.imwrite(filename, self.current_frame)
            self.status_var.set(f"Snapshot saved: {filename}")
            messagebox.showinfo("Success", f"Snapshot saved as:\n{filename}")
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save snapshot:\n{str(e)}")
    
    def _on_closing(self):
        """Handle window closing event."""
        self._disconnect_camera()
        self.root.destroy()


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description='Foscam Camera Viewer - View live RTSP stream from Foscam cameras',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --ip 192.168.1.6 --user pablo --password mypass
  %(prog)s --ip 192.168.1.6 --user pablo --password mypass --port 88

Audio: Con av + sounddevice (y libportaudio) se usa un solo demux (PyAV) y vídeo/audio sincronizados.
Sin ellos se usa ffplay en subproceso (sin sync perfecto).
        """
    )
    
    parser.add_argument(
        '--ip',
        type=str,
        required=True,
        help='Camera IP address (required)'
    )
    
    parser.add_argument(
        '--port',
        type=int,
        default=88,
        help='Camera port for RTSP (default: 88)'
    )
    
    parser.add_argument(
        '--user',
        type=str,
        required=True,
        help='Camera username (required)'
    )
    
    parser.add_argument(
        '--password',
        type=str,
        required=True,
        help='Camera password (required)'
    )
    
    parser.add_argument(
        '--sub',
        action='store_true',
        help='Use sub stream (videoSub) for lower resolution / less bandwidth'
    )
    
    parser.add_argument(
        '--audio-gate-db',
        type=float,
        default=-38,
        metavar='dB',
        help=(
            'Puerta de ruido: solo se oye el audio por ENCIMA de este nivel (dB). '
            'Por debajo → silencio; por encima → se escucha normal. '
            'Por defecto -38 (deja llanto/ruidos fuertes, corta música suave). '
            'Ej: -40 más permisivo; -30 solo sonidos muy fuertes. Use un valor muy bajo (ej. -90) para desactivar.'
        ),
    )
    
    parser.add_argument(
        '--nvidia',
        action='store_true',
        help='Decodificar video con GPU NVIDIA (GStreamer nvh264dec). Requiere OpenCV con GStreamer y plugins nvcodec. Si falla, se usa FFmpeg (CPU).'
    )
    
    args = parser.parse_args()
    
    # Validate IP address
    if not args.ip or not args.ip.strip():
        print("Error: IP address is required", file=sys.stderr)
        sys.exit(1)
    
    # Create GUI
    root = tk.Tk()
    app = FoscamViewer(
        root, args.ip.strip(), args.port, args.user, args.password,
        use_sub_stream=args.sub,
        audio_gate_db=args.audio_gate_db,
        use_nvidia_decode=args.nvidia,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
