#!/usr/bin/env python3
"""
Foscam Camera Viewer with GUI
Displays live video stream from Foscam cameras.
Takes connection parameters from command line arguments.
"""

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import cv2
from PIL import Image, ImageTk
import threading
import time
import queue
import argparse
import json
import os
import re
from pathlib import Path
import signal
import sys
import subprocess
import shutil
import tempfile
import requests
from urllib.parse import urlencode, quote
import xml.etree.ElementTree as ET

from foscam.client import FoscamClient
from foscam.ui.shell import ViewerShell
from foscam.ui import theme as ui_theme

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

MOTION_FRAME_SIZE = (160, 120)
METER_EMA_ALPHA = 0.25
AUDIO_DB_FLOOR = -60.0
AUDIO_DB_CEIL = 0.0
GATE_SLIDER_MIN = -90
GATE_SLIDER_MAX = -20
GATE_PRESETS = {"Llanto": -38.0, "Suave": -48.0, "Off": -90.0}
VIEWER_PREFS_PATH = Path.home() / ".config" / "foscam-controller" / "viewer.json"
DISPLAY_INTERVAL_MS = 33  # ~30 FPS UI; menos carga que 20 ms con CTkImage
DISPLAY_DETAIL_EVERY_N = 15  # actualizar panel técnico cada N frames mostrados


def _load_viewer_prefs():
    try:
        if VIEWER_PREFS_PATH.is_file():
            with open(VIEWER_PREFS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _save_viewer_prefs(
    volume, audio_gate_db, geometry=None, muted=None, volume_before_mute=None, ui_scale=None,
):
    try:
        VIEWER_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "volume": int(volume),
            "audio_gate_db": float(audio_gate_db),
        }
        if geometry:
            data["geometry"] = str(geometry)
        if muted is not None:
            data["muted"] = bool(muted)
        if volume_before_mute is not None:
            data["volume_before_mute"] = int(volume_before_mute)
        if ui_scale is not None:
            data["ui_scale"] = float(ui_scale)
        try:
            if VIEWER_PREFS_PATH.is_file():
                with open(VIEWER_PREFS_PATH, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing.update(data)
                    data = existing
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        with open(VIEWER_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


class FoscamViewer:
    """GUI application to view Foscam camera streams."""
    
    def __init__(
        self, root, ip, port, user, password,
        use_sub_stream=False, audio_gate_db=None, use_nvidia_decode=False,
        initial_volume=None, ui_scale=None,
    ):
        prefs = _load_viewer_prefs()
        self.ui_scale = float(
            ui_scale if ui_scale is not None
            else prefs.get("ui_scale", ui_theme.DEFAULT_UI_SCALE)
        )
        self.root = root
        self.camera_name = None  # Fetched from camera after connection
        self.root.title(f"Visor Foscam - {ip}:{port}")
        geom = prefs.get("geometry") or ui_theme.default_window_geometry(self.ui_scale)
        try:
            self.root.geometry(str(geom))
        except tk.TclError:
            self.root.geometry(ui_theme.default_window_geometry(self.ui_scale))
        self._muted = bool(prefs.get("muted", False))
        saved_vol = int(prefs.get("volume", 50))
        self._volume_before_mute = int(prefs.get("volume_before_mute", saved_vol if saved_vol > 0 else 50))
        self._fullscreen = False
        self._video_photo = None
        self._last_image_size = None
        self._display_frame_count = 0
        self._geom_save_after_id = None
        self.root.bind("<Configure>", self._on_root_configure)
        
        # Camera connection parameters (from command line)
        self.camera_ip = ip
        self.camera_user = user
        self.camera_password = password
        self.camera_port = port
        self._cgi_client = FoscamClient(ip, user, password, port)
        self.use_sub_stream = use_sub_stream  # Prefer videoSub (lower resolution)
        # Puerta de audio en dB (slider); -90 ≈ desactivada
        if audio_gate_db is not None:
            self.audio_gate_db = float(audio_gate_db)
        else:
            self.audio_gate_db = float(prefs.get("audio_gate_db", -38.0))
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
        self._use_pyav_audio_sidecar = False  # Vídeo OpenCV + audio PyAV (puerta en vivo)
        self._av_audio_container = None
        self._av_audio_sidecar_thread = None
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
        # Volumen de reproducción, 0–100
        if initial_volume is not None:
            self._playback_volume = int(initial_volume)
        else:
            self._playback_volume = int(prefs.get("volume", 50))
        self._playback_volume = max(0, min(100, self._playback_volume))
        if self._muted:
            if saved_vol > 0:
                self._volume_before_mute = saved_vol
            self._playback_volume = 0
        self._audio_restart_in_progress = False
        self._gate_restart_after_id = None
        self._prefs_save_after_id = None
        # Medidores (actualizados desde hilos de stream / CGI)
        self._audio_level = 0.0
        self._audio_level_ema = 0.0
        self._audio_level_db = -60.0
        self._audio_meter_thread = None
        self._audio_meter_stop = threading.Event()
        self._motion_level = 0.0
        self._motion_ema = 0.0
        self._prev_gray = None
        self._camera_alarm_active = None  # None = N/D, True/False
        self._cgi_poll_stop = threading.Event()
        self._cgi_poll_thread = None
        # Create GUI
        self._create_widgets()
        self._start_cgi_motion_poll()
        
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
        """Create GUI via CustomTkinter shell."""
        host = f"{self.camera_ip}:{self.camera_port}"
        self.ui = ViewerShell(self.root, host=host)
        self.ui.build(
            initial_volume=self._playback_volume,
            gate_db=self.audio_gate_db,
            gate_min=GATE_SLIDER_MIN,
            gate_max=GATE_SLIDER_MAX,
            params_display=self._build_params_display(),
            on_snapshot=self._take_snapshot,
            on_disconnect=self._disconnect_camera,
            on_mute_toggle=self._toggle_mute,
            on_volume_change=self._on_volume_slider,
            on_gate_change=self._on_gate_slider,
            on_gate_preset=self._apply_gate_preset,
            on_display_resize=self._on_display_resize,
            on_ptz_move=self._ptz_move,
            on_ptz_stop=self._ptz_stop,
            on_toggle_details=self._toggle_details_panel,
            on_toggle_help=self._toggle_help_panel,
        )
        self.video_label = self.ui.video_label
        self.status_var = self.ui.status_var
        self.resolution_var = self.ui.resolution_var
        self.display_size_var = self.ui.display_size_var
        self.decode_backend_var = self.ui.decode_backend_var
        self.volume_var = self.ui.volume_var
        self.snapshot_btn = self.ui.snapshot_btn
        self.disconnect_btn = self.ui.disconnect_btn
        self._vol_label_var = self.ui._vol_label_var
        self._gate_label_var = self.ui._gate_label_var
        self._audio_level_label_var = self.ui._audio_level_label_var
        self._camera_alarm_label_var = self.ui._camera_alarm_label_var
        self.vu_meter = self.ui.vu_meter
        self._motion_meter = self.ui._motion_meter
        self._camera_alarm_meter = self.ui._camera_alarm_meter
        self.reconnect_banner = self.ui.reconnect_banner
        self._vol_slider = self.ui.vol_slider
        self._gate_slider = self.ui.gate_slider
        self.ui.set_mute_button(self._muted)
        self._sync_volume_ui()
        self._set_connection_state("connecting")
        self._update_audio_meter_enabled()
        self.vu_meter.set_gate_db(self.audio_gate_db)
        self._update_technical_details()
        self._update_display_loop()
        self._meter_ui_loop()

    def _sync_volume_ui(self):
        vol = int(self._playback_volume)
        self._vol_slider.set(vol)
        self._vol_label_var.set(f"{vol} %")
        self.volume_var.set(f"Vol: {vol}")

    def _set_connection_state(self, state: str) -> None:
        self.ui.status_pill.set_state(state)
        if state == "reconnecting":
            self.reconnect_banner.show()
        else:
            self.reconnect_banner.hide()

    def _set_status_short(self, text: str) -> None:
        self.status_var.set(text)

    def _update_technical_details(self) -> None:
        parts = [self._build_params_display()]
        res = self.resolution_var.get()
        if res:
            parts.append(f"Resolución stream: {res}")
        disp = self.display_size_var.get()
        if disp:
            parts.append(disp)
        dec = self.decode_backend_var.get()
        if dec:
            parts.append(f"Decode: {dec}")
        parts.append(f"Audio: {self._audio_backend_label()}")
        self.ui.set_params_display("\n".join(parts))

    def _toggle_details_panel(self) -> None:
        self.ui.toggle_details()

    def _toggle_help_panel(self) -> None:
        self.ui.toggle_help()

    def _toggle_mute(self) -> None:
        if self._muted:
            self._muted = False
            self._playback_volume = max(0, min(100, self._volume_before_mute))
        else:
            self._muted = True
            if self._playback_volume > 0:
                self._volume_before_mute = self._playback_volume
            self._playback_volume = 0
        self.ui.set_mute_button(self._muted)
        self._sync_volume_ui()
        self._schedule_save_prefs()
        if not self._uses_live_audio_gate():
            self._schedule_ffplay_audio_restart()

    def _toggle_fullscreen(self, _event=None) -> None:
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            for w in (self.ui.toolbar, self.ui.sidebar, self.ui.footer):
                w.pack_forget()
            if self.ui._details_open:
                self.ui.details_panel.pack_forget()
            if self.ui._help_open:
                self.ui.help_panel.pack_forget()
            self.ui.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.ui.set_ptz_hint_visible(False)
        else:
            self.ui.set_ptz_hint_visible(True)
            self.ui.toolbar.pack(side=tk.TOP, fill=tk.X)
            self.ui.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.ui.footer.pack(side=tk.BOTTOM, fill=tk.X)
            if self.ui._details_open:
                self.ui.details_panel.pack(side=tk.BOTTOM, fill=tk.X, before=self.ui.footer)
            if self.ui._help_open:
                self.ui.help_panel.pack(side=tk.BOTTOM, fill=tk.X, before=self.ui.footer)

    def _apply_gate_preset(self, db: float) -> None:
        self.audio_gate_db = float(db)
        self._gate_slider.set(self.audio_gate_db)
        self._on_gate_slider(self.audio_gate_db)

    def _on_root_configure(self, event) -> None:
        if event.widget != self.root:
            return
        if self._geom_save_after_id is not None:
            self.root.after_cancel(self._geom_save_after_id)
        self._geom_save_after_id = self.root.after(500, self._save_window_geometry)

    def _save_window_geometry(self) -> None:
        self._geom_save_after_id = None
        try:
            geom = self.root.geometry()
            vol_save = self._volume_before_mute if self._muted else self._playback_volume
            _save_viewer_prefs(
                vol_save, self.audio_gate_db,
                geometry=geom, muted=self._muted,
                volume_before_mute=self._volume_before_mute,
                ui_scale=self.ui_scale,
            )
        except tk.TclError:
            pass

    def _samples_db(self, samples) -> float:
        """Nivel RMS en dB de un bloque de muestras (misma escala que el medidor)."""
        if np is None:
            return AUDIO_DB_FLOOR
        arr = np.asarray(samples, dtype=np.float64)
        if arr.size == 0:
            return AUDIO_DB_FLOOR
        rms = float(np.sqrt(np.mean(np.square(arr))))
        return max(AUDIO_DB_FLOOR, min(AUDIO_DB_CEIL, 20.0 * np.log10(rms + 1e-9)))

    def _gate_is_open(self, samples=None) -> bool:
        """True si el audio debe pasar según umbral (alineado con la línea del medidor)."""
        db_thresh = float(self.audio_gate_db)
        if db_thresh <= GATE_SLIDER_MIN + 1:
            return True
        level_db = float(getattr(self, "_audio_level_db", AUDIO_DB_FLOOR))
        if samples is not None and np is not None:
            chunk_db = self._samples_db(samples)
            if max(chunk_db, level_db) >= db_thresh:
                return True
            return False
        return level_db >= db_thresh

    def _apply_audio_gate(self, samples):
        """Puerta en dB: usa EMA del medidor + pico del bloque actual."""
        if self._gate_is_open(samples):
            return samples
        if np is None:
            return samples
        arr = np.asarray(samples, dtype=np.float32)
        return np.zeros_like(arr)

    def _ffplay_audio_filter(self):
        """Filtro FFmpeg para ffplay; None si el umbral está desactivado."""
        db = float(self.audio_gate_db)
        if db <= GATE_SLIDER_MIN + 1:
            return None
        th_lin = max(1e-6, min(1.0, 10 ** (db / 20.0)))
        return (
            f"agate=threshold={th_lin:.8f}:ratio=9000:range=1:"
            f"attack=0.01:release=0.05"
        )

    def _audio_backend_label(self) -> str:
        if getattr(self, "_use_pyav", False):
            return "PyAV sync (umbral en vivo)"
        if getattr(self, "_use_pyav_audio_sidecar", False):
            return "PyAV + altavoz (umbral en vivo)"
        if self._audio_process is not None and self._audio_process.poll() is None:
            return "ffplay (umbral al reiniciar audio)"
        if shutil.which("ffplay"):
            return "ffplay"
        return "sin audio"

    def _uses_live_audio_gate(self):
        """True si la puerta se aplica en el callback (PyAV), no vía ffplay."""
        return self._use_pyav or self._use_pyav_audio_sidecar

    def _update_audio_meter_enabled(self):
        if getattr(self, "vu_meter", None) is None:
            return
        pyav_audio = self._uses_live_audio_gate() and getattr(self, "_audio_queue", None) is not None
        ffplay_meter = (
            not self._uses_live_audio_gate()
            and PYAV_AUDIO_AVAILABLE
            and self.is_streaming
            and getattr(self, "_audio_meter_thread", None) is not None
        )
        if pyav_audio or ffplay_meter:
            self.vu_meter.set_enabled(True)
            if not pyav_audio and ffplay_meter:
                self._audio_level_label_var.set("— dB (demux aux.)")
            else:
                self._audio_level_label_var.set("— dB")
        else:
            self.vu_meter.set_enabled(False)
            if not AUDIO_AVAILABLE:
                self._audio_level_label_var.set("Sin audio")
            elif not PYAV_AUDIO_AVAILABLE:
                self._audio_level_label_var.set("Requiere PyAV")
            else:
                self._audio_level_label_var.set("Sin stream de audio")

    def _schedule_save_prefs(self):
        if self._prefs_save_after_id is not None:
            self.root.after_cancel(self._prefs_save_after_id)
        self._prefs_save_after_id = self.root.after(
            500, lambda: self._do_save_prefs(),
        )

    def _do_save_prefs(self):
        self._prefs_save_after_id = None
        try:
            geom = self.root.geometry()
        except tk.TclError:
            geom = None
        vol_save = self._volume_before_mute if self._muted else self._playback_volume
        _save_viewer_prefs(
            vol_save, self.audio_gate_db,
            geometry=geom, muted=self._muted,
            volume_before_mute=self._volume_before_mute,
            ui_scale=self.ui_scale,
        )

    def _on_volume_slider(self, value=None):
        vol = int(round(float(value if value is not None else self._vol_slider.get())))
        vol = max(0, min(100, vol))
        if vol == self._playback_volume and not self._muted:
            return
        if vol > 0:
            self._muted = False
            self.ui.set_mute_button(False)
            self._volume_before_mute = vol
        self._playback_volume = vol
        self._vol_label_var.set(f"{vol} %")
        self.volume_var.set(f"Vol: {vol}")
        self._schedule_save_prefs()
        if self._uses_live_audio_gate():
            return
        self._schedule_ffplay_audio_restart()

    def _on_gate_slider(self, value=None):
        self.audio_gate_db = float(value if value is not None else self._gate_slider.get())
        self._gate_label_var.set(f"{self.audio_gate_db:.0f} dB")
        self.vu_meter.set_gate_db(self.audio_gate_db)
        self.ui.highlight_gate_preset(self.audio_gate_db)
        self._schedule_save_prefs()
        self._update_technical_details()
        if self._uses_live_audio_gate():
            return
        self._schedule_ffplay_audio_restart()

    def _schedule_ffplay_audio_restart(self):
        if self._uses_live_audio_gate() or not AUDIO_AVAILABLE or not self.rtsp_url or not self.is_streaming:
            return
        if self._gate_restart_after_id is not None:
            self.root.after_cancel(self._gate_restart_after_id)
        self._gate_restart_after_id = self.root.after(200, self._do_ffplay_audio_restart)

    def _do_ffplay_audio_restart(self):
        self._gate_restart_after_id = None
        if self._uses_live_audio_gate() or not self.is_streaming:
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

    def _update_audio_level_from_samples(self, arr):
        if np is None or arr is None or arr.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(arr.astype(np.float64)))))
        db = 20.0 * np.log10(rms + 1e-9)
        db = max(AUDIO_DB_FLOOR, min(AUDIO_DB_CEIL, db))
        span = AUDIO_DB_CEIL - AUDIO_DB_FLOOR
        level = ((db - AUDIO_DB_FLOOR) / span) * 100.0 if span > 0 else 0.0
        level = max(0.0, min(100.0, level))
        self._audio_level_ema = (
            METER_EMA_ALPHA * level + (1.0 - METER_EMA_ALPHA) * self._audio_level_ema
        )
        self._audio_level = self._audio_level_ema
        self._audio_level_db = db

    def _try_start_pyav_audio_sidecar(self, rtsp_url):
        """Audio PyAV + sounddevice con vídeo OpenCV (puerta y volumen en vivo)."""
        if not PYAV_AUDIO_AVAILABLE:
            return False
        container = None
        try:
            opts = {"rtsp_flags": "prefer_tcp", "fflags": "nobuffer", "flags": "low_delay"}
            container = av.open(rtsp_url, options=opts)
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
            if audio_stream is None:
                container.close()
                return False
            self._av_audio_container = container
            self._av_audio_stream = audio_stream
            self._use_pyav_audio_sidecar = True
            sr = int(audio_stream.sample_rate)
            ch = audio_stream.layout.channels if hasattr(audio_stream.layout, "channels") else 1
            self._sd_channels = ch
            self._audio_queue = queue.Queue(maxsize=min(2048, max(32, sr // 10)))
            self._sd_stream = sd.OutputStream(
                samplerate=sr, channels=ch, dtype="float32",
                blocksize=1024, callback=self._sd_audio_callback,
            )
            self._sd_stream.start()
            self._av_audio_sidecar_thread = threading.Thread(
                target=self._av_audio_sidecar_demux_thread, daemon=True,
            )
            self._av_audio_sidecar_thread.start()
            return True
        except Exception:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self._stop_pyav_audio_sidecar()
            return False

    def _stop_pyav_audio_sidecar(self):
        self._use_pyav_audio_sidecar = False
        self._audio_queue = None
        if self._sd_stream is not None:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        t = getattr(self, "_av_audio_sidecar_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._av_audio_sidecar_thread = None
        if self._av_audio_container is not None:
            try:
                self._av_audio_container.close()
            except Exception:
                pass
            self._av_audio_container = None
        self._av_audio_stream = None

    def _av_audio_sidecar_demux_thread(self):
        container = self._av_audio_container
        audio_stream = self._av_audio_stream
        audio_queue = self._audio_queue
        try:
            for packet in container.demux(audio_stream):
                if not self.is_streaming or not self._use_pyav_audio_sidecar:
                    break
                for frame in packet.decode():
                    if not self.is_streaming:
                        break
                    try:
                        arr = frame.to_ndarray()
                        if arr.dtype != np.float32:
                            arr = arr.astype(np.float32) / 32768.0
                        self._update_audio_level_from_samples(arr)
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
            if self.is_streaming and self._use_pyav_audio_sidecar:
                self.root.after(0, lambda err=str(e): self._set_status_short(f"Audio: {err}"))
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self._av_audio_container = None

    def _start_audio_level_meter(self):
        """Demux PyAV solo para medir nivel cuando el audio va por ffplay."""
        if self._uses_live_audio_gate() or not PYAV_AUDIO_AVAILABLE or not self.rtsp_url:
            return
        self._stop_audio_level_meter()
        self._audio_meter_stop.clear()
        self._audio_meter_thread = threading.Thread(
            target=self._audio_level_meter_loop, daemon=True,
        )
        self._audio_meter_thread.start()
        self.root.after(0, self._update_audio_meter_enabled)

    def _stop_audio_level_meter(self):
        self._audio_meter_stop.set()
        t = getattr(self, "_audio_meter_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._audio_meter_thread = None

    def _audio_level_meter_loop(self):
        container = None
        try:
            opts = {"rtsp_flags": "prefer_tcp", "fflags": "nobuffer", "flags": "low_delay"}
            container = av.open(self.rtsp_url, options=opts)
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
            if audio_stream is None:
                return
            for packet in container.demux(audio_stream):
                if self._audio_meter_stop.is_set() or not self.is_streaming or self._uses_live_audio_gate():
                    break
                for frame in packet.decode():
                    if self._audio_meter_stop.is_set():
                        break
                    try:
                        arr = frame.to_ndarray()
                        if arr.dtype != np.float32:
                            arr = arr.astype(np.float32) / 32768.0
                        self._update_audio_level_from_samples(arr)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

    def _compute_motion_level(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, MOTION_FRAME_SIZE, interpolation=cv2.INTER_AREA)
        if self._prev_gray is None:
            self._prev_gray = small
            return 0.0
        diff = cv2.absdiff(self._prev_gray, small)
        self._prev_gray = small
        raw = float(cv2.mean(diff)[0])
        # Escala empírica: diff medio ~0–25 suele ser poco/mucho movimiento
        instant = max(0.0, min(100.0, (raw / 25.0) * 100.0))
        self._motion_ema = (
            METER_EMA_ALPHA * instant + (1.0 - METER_EMA_ALPHA) * self._motion_ema
        )
        return self._motion_ema

    def _meter_ui_loop(self):
        try:
            self.vu_meter.redraw(self._audio_level)
            gate_db = self.audio_gate_db
            self._audio_level_label_var.set(
                f"{self._audio_level_db:.1f} dB (umbral {gate_db:.0f})"
            )
            self._motion_meter.set(self._motion_level / 100.0)
            if self._camera_alarm_active is None:
                self._camera_alarm_meter.set(0)
                self._camera_alarm_label_var.set("N/D")
                self.ui.set_alarm_badge(None)
            elif self._camera_alarm_active:
                self._camera_alarm_meter.set(1.0)
                self._camera_alarm_label_var.set("Activa")
                self.ui.set_alarm_badge(True)
            else:
                self._camera_alarm_meter.set(0)
                self._camera_alarm_label_var.set("Inactiva")
                self.ui.set_alarm_badge(False)
        except tk.TclError:
            pass
        self.root.after(100, self._meter_ui_loop)

    def _parse_cgi_motion_alarm(self, xml_text):
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        motion_keys = (
            "motion", "alarm", "detect", "md", "ioalarm", "io_alarm",
        )
        for el in root.iter():
            tag = (el.tag or "").split("}")[-1].lower()
            text = (el.text or "").strip()
            if not text:
                continue
            if any(k in tag for k in motion_keys):
                if text in ("1", "true", "yes", "on"):
                    return True
                if text in ("0", "false", "no", "off"):
                    return False
        return None

    def _cgi_motion_poll_loop(self):
        while not self._cgi_poll_stop.is_set() and self.is_streaming:
            active = None
            try:
                for cmd in ("getDevState", "getMotionDetectConfig", "getIOAlarmConfig"):
                    if self._cgi_poll_stop.is_set():
                        break
                    resp = self._cgi_client.send(cmd)
                    parsed = self._parse_cgi_motion_alarm(resp)
                    if parsed is not None:
                        active = parsed
                        break
            except Exception:
                pass
            self._camera_alarm_active = active
            if self._cgi_poll_stop.wait(1.5):
                break

    def _start_cgi_motion_poll(self):
        self._cgi_poll_stop.clear()
        self._cgi_poll_thread = threading.Thread(target=self._cgi_motion_poll_loop, daemon=True)
        self._cgi_poll_thread.start()
    
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
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", lambda e: self._exit_fullscreen_if_active())
        self.root.focus_set()
        self.root.bind("<FocusIn>", lambda e: self.root.focus_set())

    def _exit_fullscreen_if_active(self) -> None:
        if self._fullscreen:
            self._toggle_fullscreen()
    
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
        """Sube o baja el volumen (atajos a/z). Sincroniza slider; ffplay reinicia si aplica."""
        new_vol = max(0, min(100, self._playback_volume + delta * 10))
        if new_vol == self._playback_volume:
            return
        self._playback_volume = new_vol
        self.volume_var.set(f"Vol: {new_vol}")
        if hasattr(self, "_vol_slider"):
            self._vol_slider.set(new_vol)
            self._vol_label_var.set(f"{new_vol} %")
        if new_vol > 0 and self._muted:
            self._muted = False
            self.ui.set_mute_button(False)
        if self._uses_live_audio_gate():
            return
        self._schedule_ffplay_audio_restart()

    def _ptz_reset(self):
        """Envía comando ptzReset (posición por defecto)."""
        self._ptz_cgi("ptzReset")
    
    def _connect_camera(self):
        """Connect to the camera and start streaming."""
        self._set_connection_state("connecting")
        self._set_status_short(f"Conectando a {self.camera_ip}:{self.camera_port}…")
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
                self.root.after(0, lambda: self._set_connection_state("connecting"))
                self.root.after(0, lambda url=rtsp_url: self._set_status_short(
                    f"Conectando RTSP {url.split('@')[1]}…",
                ))
                
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
                            self.root.after(0, lambda url=rtsp_url: self._set_status_short(
                                f"RTSP PyAV {url.split('@')[1]}",
                            ))
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
                            self.root.after(0, self._update_audio_meter_enabled)
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
                        self.root.after(0, lambda url=rtsp_url: self._set_status_short(
                            f"RTSP {url.split('@')[1]}",
                        ))
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
                            live_audio = self._try_start_pyav_audio_sidecar(rtsp_url)
                            if not live_audio:
                                self.audio_thread = threading.Thread(
                                    target=self._audio_playback_thread, daemon=True,
                                )
                                self.audio_thread.start()
                                self.root.after(0, self._start_audio_level_meter)
                        self.root.after(0, self._update_audio_meter_enabled)
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
        self.root.after(0, lambda: self._connection_failed(
            "No se pudo conectar por RTSP. Comprueba:\n"
            "- Puerto (88 habitual en Foscam)\n"
            "- Usuario y contraseña\n"
            "- URL: rtsp://usuario:pass@ip:88/videoMain"
        ))
    
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
                            self.root.after(0, lambda n=name: self.ui.set_camera_title(n))
            except Exception:
                pass
        threading.Thread(target=do_fetch, daemon=True).start()
    
    def _sd_audio_callback(self, outdata, frames, time_info, status):
        """Callback sounddevice: puerta y volumen en tiempo real (modo PyAV)."""
        if status:
            pass
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
                samples = self._apply_audio_gate(chunk)
                n = min(len(samples), need - filled)
                out[filled:filled + n] = samples[:n] * vol
                filled += n
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
                            self._update_audio_level_from_samples(arr)
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
                self.root.after(0, lambda err=str(e): self._set_status_short(f"Stream: {err}"))
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
            af = self._ffplay_audio_filter()
            if af:
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
                self.root.after(0, lambda err=str(e): self._set_status_short(f"Audio: {err}"))
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
                        self.root.after(0, lambda: self._set_connection_state("reconnecting"))
                        self.root.after(0, lambda: self._set_status_short(
                            "Stream interrumpido, reconectando…",
                        ))
                        time.sleep(2)
                        # Try to reconnect
                        if self.is_streaming:
                            self._start_stream()
                        break
                time.sleep(read_interval)
            except Exception as e:
                if self.is_streaming:
                    self.root.after(0, lambda: self._set_connection_state("reconnecting"))
                    self.root.after(0, lambda err=str(e): self._set_status_short(f"Stream error: {err}"))
                time.sleep(1)
                if not self.is_streaming:
                    break
    
    def _on_display_resize(self, event):
        """When window/display area is resized, use new size for scaling only (no automatic resolution change)."""
        if event.widget != self.video_label or event.width < 2 or event.height < 2:
            return
        self._display_size = (event.width, event.height)

    def _update_display_loop(self):
        """Actualiza el vídeo con el último frame de la cola (ImageTk, más liviano que CTkImage)."""
        try:
            frame = None
            while True:
                try:
                    frame = self.frame_queue.get_nowait()
                except queue.Empty:
                    break

            if frame is not None:
                if self._display_size and self._display_size[0] > 1 and self._display_size[1] > 1:
                    display_width, display_height = self._display_size
                else:
                    display_width = self.video_label.winfo_width()
                    display_height = self.video_label.winfo_height()

                if display_width > 1 and display_height > 1:
                    height, width = frame.shape[:2]
                    scale = min(display_width / width, display_height / height)
                    new_width = max(1, int(width * scale))
                    new_height = max(1, int(height * scale))
                    interp = cv2.INTER_AREA if new_width < width else cv2.INTER_LINEAR
                    frame_resized = cv2.resize(
                        frame, (new_width, new_height), interpolation=interp,
                    )
                else:
                    frame_resized = frame
                    new_width, new_height = frame.shape[1], frame.shape[0]

                frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(frame_rgb)
                size = (new_width, new_height)
                self._video_photo = ImageTk.PhotoImage(image=image)
                self._last_image_size = size
                self.video_label.configure(image=self._video_photo, text="")
                self.video_label.image = self._video_photo

                self.current_frame = frame
                self._motion_level = self._compute_motion_level(frame)
                h, w = frame.shape[:2]
                self.resolution_var.set(f"{w}×{h}")
                if display_width > 1 and display_height > 1:
                    self.display_size_var.set(f"Display {display_width}×{display_height}")
                self._display_frame_count += 1
                if self._display_frame_count % DISPLAY_DETAIL_EVERY_N == 0:
                    self._update_technical_details()

        except Exception as e:
            if self.is_streaming:
                self._set_status_short(f"Display error: {str(e)}")

        self.root.after(DISPLAY_INTERVAL_MS, self._update_display_loop)
    
    def _update_ui_connected(self):
        """Update UI after successful connection."""
        self.root.after(0, lambda: self._set_connection_state("live"))
        parts = [f"En vivo · {self.camera_ip}:{self.camera_port}"]
        if self.use_sub_stream:
            parts.append("sub stream")
        if AUDIO_AVAILABLE:
            parts.append("vídeo + audio")
        self._set_status_short(" · ".join(parts))
        self.root.after(0, self._update_audio_meter_enabled)
        backend = getattr(self, "_decode_backend", "ffmpeg")
        if backend == "nvidia":
            self.decode_backend_var.set("NVIDIA")
        elif self.use_nvidia_decode:
            self.decode_backend_var.set("NVIDIA✗")
        else:
            self.decode_backend_var.set("")
        self.root.after(0, self._update_technical_details)

    def _connection_failed(self, error_msg):
        """Handle connection failure."""
        self._set_connection_state("error")
        self._set_status_short(f"Error de conexión: {error_msg}")
        messagebox.showerror("Error de conexión", f"No se pudo conectar a la cámara:\n{error_msg}")
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
        self._cgi_poll_stop.set()
        self._stop_audio_level_meter()
        self._prev_gray = None
        self._motion_level = 0.0
        self._motion_ema = 0.0
        self._audio_level = 0.0
        self._camera_alarm_active = None
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
            if getattr(self, "_use_pyav_audio_sidecar", False):
                self._stop_pyav_audio_sidecar()
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
        self.video_label.configure(image="", text="Desconectado")
        self.video_label.image = None
        self._video_photo = None
        self._last_image_size = None
        self._set_connection_state("offline")
        self._set_status_short("Desconectado")
        self.resolution_var.set("")
        self.display_size_var.set("")
        self._update_technical_details()
    
    def _take_snapshot(self):
        """Take a snapshot and save it."""
        if not self.is_streaming or not hasattr(self, 'current_frame') or self.current_frame is None:
            messagebox.showwarning("Aviso", "No hay stream activo para capturar")
            return
        
        try:
            # Generate filename with timestamp
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot_{self.camera_ip}_{timestamp}.jpg"
            
            # Save frame
            cv2.imwrite(filename, self.current_frame)
            self._set_status_short(f"Captura guardada: {filename}")
            messagebox.showinfo("Listo", f"Captura guardada en:\n{filename}")
            
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo guardar la captura:\n{str(e)}")
    
    def _on_closing(self):
        """Handle window closing event."""
        self._save_window_geometry()
        self._do_save_prefs()
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
        default=None,
        metavar='dB',
        help=(
            'Umbral de ruido: solo se oye el audio por ENCIMA de este nivel (dB). '
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

    parser.add_argument(
        '--ui-scale',
        type=float,
        default=None,
        metavar='FACTOR',
        help=(
            'Escala de la interfaz CustomTkinter (default: valor en viewer.json o 2.0). '
            'Ej: 1.5, 2.0, 2.5. Afecta controles y tamaño sugerido de ventana.'
        ),
    )
    
    args = parser.parse_args()
    
    # Validate IP address
    if not args.ip or not args.ip.strip():
        print("Error: IP address is required", file=sys.stderr)
        sys.exit(1)
    
    prefs = _load_viewer_prefs()
    ui_scale = getattr(args, "ui_scale", None)
    if ui_scale is None:
        ui_scale = prefs.get("ui_scale", ui_theme.DEFAULT_UI_SCALE)
    ui_theme.apply_theme(float(ui_scale))
    root = ctk.CTk()
    app = FoscamViewer(
        root, args.ip.strip(), args.port, args.user, args.password,
        use_sub_stream=args.sub,
        audio_gate_db=args.audio_gate_db,
        use_nvidia_decode=args.nvidia,
        ui_scale=ui_scale,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
