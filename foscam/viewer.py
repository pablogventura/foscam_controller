#!/usr/bin/env python3
"""
Foscam Camera Viewer with GUI
Displays live video stream from Foscam cameras.
Takes connection parameters from command line arguments.
"""

import tkinter as tk
from dataclasses import dataclass
from typing import Optional
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

from foscam.audio_gate import (
    AUDIO_DB_CEIL,
    AUDIO_DB_FLOOR,
    GATE_PRESETS,
    GATE_SLIDER_MAX,
    GATE_SLIDER_MIN,
    apply_gate,
    ffplay_agate_filter,
    gate_is_open,
    process_playback_chunk,
    samples_db,
)
from foscam.client import FoscamClient
from foscam.motion import (
    AutoZoomController,
    HeatmapAccumulator,
    MotionAnalyzer,
    MotionSettings,
    ZonesOverlayCache,
    draw_motion_overlays,
    load_motion_settings,
    parse_motion_detect_zones,
    parse_osd_mask_areas,
    save_motion_to_prefs,
)
from foscam.display_pacing import (
    OVERLAY_EVERY_N_FRAMES,
    READER_QUEUE_FULL_SLEEP_SEC,
    cap_display_size,
    motion_analysis_needed,
    next_display_delay_ms,
    should_run_motion_analysis,
    visual_overlays_needed,
)
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
VIEWER_PREFS_PATH = Path.home() / ".config" / "foscam-controller" / "viewer.json"
GATE_DEBUG_EVERY_N = 50
DISPLAY_DETAIL_EVERY_N = 15  # actualizar panel técnico cada N frames mostrados


@dataclass
class _PreparedDisplay:
    rgb: object
    stream_w: int
    stream_h: int
    display_label_w: int
    display_label_h: int
    zoom_x: int = 1
    zoom_y: int = 1


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
    hud_mode=None, sidebar_collapsed=None,
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
        if hud_mode is not None:
            data["hud_mode"] = str(hud_mode)
        if sidebar_collapsed is not None:
            data["sidebar_collapsed"] = bool(sidebar_collapsed)
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


def _stream_channel_count(audio_stream) -> int:
    """Número de canales para sounddevice (layout.channels es tupla, no int)."""
    ch = getattr(audio_stream, "channels", None)
    if isinstance(ch, int) and ch > 0:
        return ch
    layout = getattr(audio_stream, "layout", None)
    if layout is not None:
        nb = getattr(layout, "nb_channels", None)
        if isinstance(nb, int) and nb > 0:
            return nb
        chs = getattr(layout, "channels", None)
        if chs is not None:
            try:
                n = len(chs)
                if n > 0:
                    return n
            except TypeError:
                pass
    return 1


def probe_nvh264dec_available() -> bool:
    """True si gst-inspect encuentra nvh264dec (GPU NVIDIA)."""
    try:
        r = subprocess.run(
            ["gst-inspect-1.0", "nvh264dec"],
            capture_output=True,
            timeout=5,
            env={**os.environ, "GST_INSPECT_NO_COLORS": "1"},
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


class FoscamViewer:
    """GUI application to view Foscam camera streams."""
    
    def __init__(
        self, root, ip, port, user, password,
        use_sub_stream=False, audio_gate_db=None, use_nvidia_decode=False,
        nvidia_source="off", initial_volume=None, ui_scale=None,
        audio_gate_debug=False,
        motion_cli_overrides=None,
    ):
        prefs = _load_viewer_prefs()
        self.motion_settings = load_motion_settings(prefs, motion_cli_overrides)
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
        self._hud_mode = str(prefs.get("hud_mode", "full"))
        if self._hud_mode not in ("full", "minimal"):
            self._hud_mode = "full"
        self._sidebar_collapsed = bool(prefs.get("sidebar_collapsed", False))
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
        self.nvidia_source = nvidia_source
        self._audio_gate_debug = bool(
            audio_gate_debug or os.environ.get("FOSCAM_AUDIO_GATE_DEBUG"),
        )
        self._gate_open_state = None
        self._gate_debug_blocks = 0
        self._gate_debug_pass = 0
        self._last_ffplay_cmd = None
        self._last_sidecar_error = None
        self._audio_ffplay_fallback = False
        
        # Video stream
        self.cap = None
        self.is_streaming = False
        self.rtsp_url = None  # Set when video connects, used for audio
        self.frame_queue = queue.Queue(maxsize=2)  # Small queue to keep frames fresh
        self.display_queue = queue.Queue(maxsize=1)
        self._last_paint_monotonic = 0.0
        self._display_fps = 0.0
        self._display_fps_window_start = time.monotonic()
        self._display_fps_window_count = 0
        self._decode_fps = 0.0
        self._decode_fps_window_start = time.monotonic()
        self._decode_fps_window_count = 0
        self._last_motion_analysis_monotonic = 0.0
        self._motion_display_frame_index = 0
        self._prep_frame_index = 0
        self._photo_size = None
        self._rgb_buffer_a = None
        self._rgb_buffer_b = None
        self._rgb_ping = False
        self._display_prep_stop = threading.Event()
        self._display_prep_thread = threading.Thread(
            target=self._display_prep_loop, daemon=True,
        )
        self._display_prep_thread.start()
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
        self._shutting_down = False
        self._ui_active = True
        self._close_started = False
        # Medidores (actualizados desde hilos de stream / CGI)
        self._audio_level = 0.0
        self._audio_level_ema = 0.0
        self._audio_level_db = AUDIO_DB_FLOOR
        self._audio_level_db_ema = float(AUDIO_DB_FLOOR)
        self._audio_meter_thread = None
        self._audio_meter_stop = threading.Event()
        self._motion_level = 0.0
        self._motion_ema = 0.0
        self._prev_gray = None
        self._camera_alarm_active = None  # None = N/D, True/False
        self._cgi_poll_stop = threading.Event()
        self._cgi_poll_thread = None
        self._motion_analyzer = MotionAnalyzer(self.motion_settings)
        self._auto_zoom = AutoZoomController(self.motion_settings)
        self._heatmap = HeatmapAccumulator(self.motion_settings.heatmap)
        self._zones_overlay_cache = ZonesOverlayCache()
        self._md_info = None
        self._privacy_masks = []
        self._motion_boxes: list = []
        self._zones_last_refresh = 0.0
        self._alarm_flash_on = False
        self._alarm_flash_ts = 0.0
        # Create GUI
        self._create_widgets()
        self._start_cgi_motion_poll()
        self._digital_crop = None
        self._start_auto_zoom_tick()
        self._refresh_motion_zones(force=True)
        threading.Thread(target=self._probe_camera_motion_caps, daemon=True).start()
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
            on_toggle_details=self._toggle_details_panel,
            on_toggle_help=self._toggle_help_panel,
            on_toggle_hud=self._toggle_hud,
            on_toggle_sidebar=self._toggle_sidebar_collapsed,
            motion=self._motion_ui_dict(),
            on_motion_change=self._on_motion_setting,
        )
        self.ui.set_hud_mode(self._hud_mode)
        if self._sidebar_collapsed:
            self.ui.set_sidebar_collapsed(True)
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
        self.ui.sidebar_mini_vu.set_gate_db(self.audio_gate_db)
        self.ui.indicator_strip.vu_meter.set_gate_db(self.audio_gate_db)
        self._update_technical_details()
        self._update_display_loop()
        self._meter_ui_loop()

    def _sync_volume_ui(self):
        vol = int(self._playback_volume)
        self._vol_slider.set(vol)
        self._vol_label_var.set(f"{vol} %")
        self.volume_var.set(f"Vol: {vol}")

    def _set_connection_state(self, state: str) -> None:
        self.ui.set_connection_state(state)
        if state == "reconnecting":
            self.ui.show_reconnect_banner()
        else:
            self.ui.hide_reconnect_banner()

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
        if self.use_nvidia_decode:
            parts.append(f"NVIDIA: {self.nvidia_source}")
        parts.append(f"Audio: {self._audio_backend_label()}")
        if self._last_ffplay_cmd:
            parts.append(f"ffplay: {self._last_ffplay_cmd}")
        if self._last_sidecar_error and not getattr(self, "_use_pyav_audio_sidecar", False):
            parts.append(f"Sidecar error: {self._last_sidecar_error}")
        if getattr(self, "_audio_ffplay_fallback", False):
            parts.append("AVISO: audio por ffplay (umbral al reiniciar)")
        if self._audio_gate_debug and self._gate_debug_blocks > 0:
            pct = 100.0 * self._gate_debug_pass / max(1, self._gate_debug_blocks)
            parts.append(
                f"Gate debug: {self._gate_debug_pass}/{self._gate_debug_blocks} "
                f"bloques abiertos ({pct:.0f}%)",
            )
        if self.is_streaming and self._display_fps > 0:
            parts.append(f"Display FPS: {self._display_fps:.1f}")
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
        try:
            self.root.attributes("-fullscreen", self._fullscreen)
        except tk.TclError:
            self.root.attributes("-zoomed", self._fullscreen)
        self.ui._apply_hud_layout()

    def _toggle_hud(self) -> None:
        self.ui.toggle_hud()
        self._hud_mode = self.ui.hud_mode
        self._schedule_save_prefs()

    def _toggle_sidebar_collapsed(self) -> None:
        self.ui.toggle_sidebar_collapsed()
        self._sidebar_collapsed = self.ui.sidebar_collapsed
        self._schedule_save_prefs()

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
            self._save_viewer_prefs_now(geometry=geom)
        except tk.TclError:
            pass

    def _save_viewer_prefs_now(self, geometry=None):
        try:
            geom = geometry or self.root.geometry()
        except tk.TclError:
            geom = None
        vol_save = self._volume_before_mute if self._muted else self._playback_volume
        _save_viewer_prefs(
            vol_save, self.audio_gate_db,
            geometry=geom, muted=self._muted,
            volume_before_mute=self._volume_before_mute,
            ui_scale=self.ui_scale,
            hud_mode=self._hud_mode,
            sidebar_collapsed=self._sidebar_collapsed,
        )
        save_motion_to_prefs(VIEWER_PREFS_PATH, self.motion_settings)

    def _samples_db(self, samples) -> float:
        if np is None:
            return AUDIO_DB_FLOOR
        return samples_db(samples)

    def _gate_is_open(self, samples=None, *, for_playback: bool = False) -> bool:
        chunk_db = self._samples_db(samples) if samples is not None else None
        level_db = float(getattr(self, "_audio_level_db", AUDIO_DB_FLOOR))
        return gate_is_open(
            self.audio_gate_db,
            chunk_db=chunk_db,
            level_db=level_db,
            chunk_only=for_playback,
        )

    def _apply_audio_gate(self, samples):
        if np is None:
            return samples
        return apply_gate(
            samples,
            self.audio_gate_db,
            level_db=float(getattr(self, "_audio_level_db", AUDIO_DB_FLOOR)),
            chunk_only=True,
        )

    def _ffplay_audio_filter(self):
        return ffplay_agate_filter(self.audio_gate_db)

    def _log_gate_debug(self, chunk_db: float, gate_open: bool) -> None:
        if not self._audio_gate_debug:
            return
        self._gate_debug_blocks += 1
        if gate_open:
            self._gate_debug_pass += 1
        if self._gate_debug_blocks % GATE_DEBUG_EVERY_N != 0:
            return
        pct = 100.0 * self._gate_debug_pass / max(1, self._gate_debug_blocks)
        level_db = float(getattr(self, "_audio_level_db", AUDIO_DB_FLOOR))
        print(
            f"[gate] backend={self._audio_backend_label()} "
            f"thresh={self.audio_gate_db:.0f} chunk={chunk_db:.1f} "
            f"level={level_db:.1f} open={gate_open} pass_pct={pct:.0f}",
            file=sys.stderr,
        )
        try:
            self.root.after(0, self._update_technical_details)
        except (tk.TclError, AttributeError):
            pass

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
            self.ui.indicator_strip.vu_meter.set_enabled(True)
            self.ui.sidebar_mini_vu.set_enabled(True)
            if not pyav_audio and ffplay_meter:
                self._audio_level_label_var.set("— dB (demux aux.)")
                self.ui.indicator_strip._audio_level_label_var.set("— dB (demux aux.)")
            else:
                self._audio_level_label_var.set("— dB")
                self.ui.indicator_strip._audio_level_label_var.set("— dB")
        else:
            self.vu_meter.set_enabled(False)
            self.ui.indicator_strip.vu_meter.set_enabled(False)
            self.ui.sidebar_mini_vu.set_enabled(False)
            if not AUDIO_AVAILABLE:
                label = "Sin audio"
            elif not PYAV_AUDIO_AVAILABLE:
                label = "Requiere PyAV"
            else:
                label = "Sin stream de audio"
            self._audio_level_label_var.set(label)
            self.ui.indicator_strip._audio_level_label_var.set(label)
            self.ui._sidebar_mini_db_var.set(label)

    def _schedule_save_prefs(self):
        if self._prefs_save_after_id is not None:
            self.root.after_cancel(self._prefs_save_after_id)
        self._prefs_save_after_id = self.root.after(
            500, lambda: self._do_save_prefs(),
        )

    def _do_save_prefs(self):
        self._prefs_save_after_id = None
        self._save_viewer_prefs_now()

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
        self.ui.sidebar_mini_vu.set_gate_db(self.audio_gate_db)
        self.ui.indicator_strip.vu_meter.set_gate_db(self.audio_gate_db)
        self.ui.highlight_gate_preset(self.audio_gate_db)
        self._schedule_save_prefs()
        self._update_technical_details()
        if self._audio_gate_debug and not self._uses_live_audio_gate():
            print(
                f"[gate] slider→{self.audio_gate_db:.0f} scheduling ffplay restart "
                f"filter={self._ffplay_audio_filter()!r}",
                file=sys.stderr,
            )
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
        if (
            self._uses_live_audio_gate()
            or not self.is_streaming
            or self._shutting_down
        ):
            return
        if self._audio_restart_in_progress:
            return
        self._audio_restart_in_progress = True

        def do_restart():
            try:
                self._audio_stop.set()
                self._terminate_audio_process()
                if self.audio_thread and self.audio_thread.is_alive():
                    self.audio_thread.join(timeout=2.5)
                if not self.is_streaming or self._shutting_down:
                    return
                self._audio_stop.clear()
                self.audio_thread = threading.Thread(
                    target=self._audio_playback_thread, daemon=True,
                )
                self.audio_thread.start()
                if self._audio_gate_debug:
                    print(
                        f"[gate] ffplay reiniciado gate={self.audio_gate_db:.0f} "
                        f"filter={self._ffplay_audio_filter()!r}",
                        file=sys.stderr,
                    )
            finally:
                self._audio_restart_in_progress = False

        threading.Thread(target=do_restart, daemon=True).start()

    def _update_audio_level_from_samples(self, arr):
        """Actualiza medidor desde un bloque (misma ventana que los chunks del gate)."""
        if np is None or arr is None or arr.size == 0:
            return
        db = self._samples_db(arr)
        self._audio_level_db = db
        self._audio_level_db_ema = (
            METER_EMA_ALPHA * db + (1.0 - METER_EMA_ALPHA) * self._audio_level_db_ema
        )
        from foscam.audio_gate import db_to_meter_ratio

        self._audio_level = db_to_meter_ratio(self._audio_level_db_ema) * 100.0

    def _try_start_pyav_audio_sidecar(self, rtsp_url):
        """Audio PyAV + sounddevice con vídeo OpenCV (puerta y volumen en vivo)."""
        self._last_sidecar_error = None
        if not PYAV_AUDIO_AVAILABLE:
            self._last_sidecar_error = "PyAV/sounddevice no disponible"
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
            ch = _stream_channel_count(audio_stream)
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
            self._audio_ffplay_fallback = False
            return True
        except Exception as exc:
            self._last_sidecar_error = str(exc)
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            self._stop_pyav_audio_sidecar()
            return False

    def _start_pyav_audio_sidecar_with_retry(self, rtsp_url, retries: int = 3) -> bool:
        for attempt in range(retries + 1):
            if attempt > 0:
                time.sleep(0.4)
            if self._try_start_pyav_audio_sidecar(rtsp_url):
                return True
            if self._audio_gate_debug:
                print(
                    f"[gate] sidecar intento {attempt + 1} falló: {self._last_sidecar_error}",
                    file=sys.stderr,
                )
        return False

    def _stop_pyav_audio_sidecar(self):
        self._use_pyav_audio_sidecar = False
        self._audio_queue = None
        container = self._av_audio_container
        if container is not None:
            self._av_audio_container = None
            try:
                container.close()
            except Exception:
                pass
        if self._sd_stream is not None:
            stream = self._sd_stream
            self._sd_stream = None
            try:
                if hasattr(stream, "abort"):
                    stream.abort()
            except Exception:
                pass
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        t = getattr(self, "_av_audio_sidecar_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._av_audio_sidecar_thread = None
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
                        ch = arr.shape[1] if arr.ndim > 1 else 1
                        step = 1024 * ch
                        for i in range(0, arr.size, step):
                            chunk = arr.ravel()[i:i + step].copy()
                            if len(chunk) > 0:
                                self._update_audio_level_from_samples(chunk)
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

    def _motion_ui_dict(self):
        s = self.motion_settings
        az = s.auto_zoom
        return {
            "show_live_overlay": s.show_live_overlay,
            "show_configured_zones": s.show_configured_zones,
            "auto_zoom_enabled": az.enabled,
            "sensitivity": s.sensitivity,
            "trigger_level": s.trigger_level,
            "trigger_hold_sec": s.trigger_hold_sec,
            "auto_zoom_return_sec": az.return_sec,
            "auto_zoom_mode": az.mode,
            "auto_zoom_ptz_speed": az.ptz_speed,
            "auto_zoom_max_digital": az.max_digital,
            "live_overlay_style": s.live_overlay_style,
            "zones_config_source": s.zones_config_source,
            "snapshot_dir": s.snapshot_on_motion.dir,
            "heatmap_decay_sec": s.heatmap.decay_sec,
            "show_legend": s.show_legend,
            "active_profile": s.active_profile,
            "profile_names": list(s.profiles.keys()),
            "debug_fsm_overlay": s.debug_fsm_overlay,
            "ignore_camera_md_disabled": s.ignore_camera_md_disabled,
            "require_audio_gate": s.require_audio_gate,
            "ignore_zones_outside_md": s.ignore_zones_outside_md,
            "flash_alarm_border": s.flash_alarm_border,
            "privacy_mask_overlay": s.privacy_mask_overlay,
            "auto_zoom": {
                "return_use_ptz_reset": az.return_use_ptz_reset,
                "return_use_zoom_out": az.return_use_zoom_out,
                "max_digital": az.max_digital,
            },
            "heatmap": {"enabled": s.heatmap.enabled, "decay_sec": s.heatmap.decay_sec},
            "snapshot_on_motion": {
                "enabled": s.snapshot_on_motion.enabled,
                "dir": s.snapshot_on_motion.dir,
            },
        }

    def _sync_motion_ui(self) -> None:
        self.ui.sync_motion_from_settings(self._motion_ui_dict())

    def _set_motion_nested(self, key: str, value) -> None:
        if key == "preset":
            self.motion_settings.apply_preset(str(value))
            self._motion_analyzer.settings = self.motion_settings
            self._auto_zoom.settings = self.motion_settings
            return
        if key == "active_profile":
            if value:
                self.motion_settings.apply_profile(str(value))
            else:
                self.motion_settings.active_profile = None
            self._motion_analyzer.settings = self.motion_settings
            self._auto_zoom.settings = self.motion_settings
            return
        if key.startswith("auto_zoom."):
            setattr(self.motion_settings.auto_zoom, key.split(".", 1)[1], value)
            self._auto_zoom.settings = self.motion_settings
            return
        if key.startswith("heatmap."):
            setattr(self.motion_settings.heatmap, key.split(".", 1)[1], value)
            self._heatmap.settings = self.motion_settings.heatmap
            return
        if key.startswith("snapshot_on_motion."):
            setattr(self.motion_settings.snapshot_on_motion, key.split(".", 1)[1], value)
            return
        if hasattr(self.motion_settings, key):
            setattr(self.motion_settings, key, value)
            self._motion_analyzer.settings = self.motion_settings
            self._auto_zoom.settings = self.motion_settings

    def _on_motion_setting(self, key: str, value) -> None:
        if key == "refresh_zones":
            self._refresh_motion_zones(force=True)
            return
        if key == "test_ptz":
            self._test_ptz_capabilities()
            return
        if key == "save_profile_prompt":
            self._prompt_save_motion_profile()
            return
        if key == "delete_profile":
            name = value
            if not name or name == "(ninguno)":
                return
            if self.motion_settings.delete_profile(name):
                self.ui.set_motion_profile_names(
                    list(self.motion_settings.profiles.keys()),
                    self.motion_settings.active_profile,
                )
                self._schedule_save_prefs()
            return
        self._set_motion_nested(key, value)
        if key in ("preset", "active_profile"):
            self._sync_motion_ui()
        if key == "privacy_mask_overlay" and value:
            self._refresh_motion_zones(force=True)
        if key in ("show_live_overlay", "show_configured_zones", "auto_zoom.enabled"):
            self.ui.sync_motion_toggles(
                self.motion_settings.show_live_overlay,
                self.motion_settings.show_configured_zones,
                self.motion_settings.auto_zoom.enabled,
            )
        self._schedule_save_prefs()

    def _prompt_save_motion_profile(self) -> None:
        dialog = ctk.CTkInputDialog(text="Nombre del perfil:", title="Guardar perfil movimiento")
        name = dialog.get_input()
        if not name or not str(name).strip():
            return
        name = str(name).strip()
        self.motion_settings.save_profile(name)
        self.ui.set_motion_profile_names(list(self.motion_settings.profiles.keys()), name)
        self._sync_motion_ui()
        self._schedule_save_prefs()

    def _refresh_motion_zones(self, force: bool = False) -> None:
        s = self.motion_settings
        now = time.time()
        if not force and s.zones_refresh_sec > 0 and (now - self._zones_last_refresh) < s.zones_refresh_sec:
            return
        self._zones_last_refresh = now
        source = s.zones_config_source
        variants = ["config", "config1", "config2"] if source == "auto" else [source.replace("config", "config") if source.startswith("config") else source]
        if source == "auto":
            variants = ["config", "config1", "config2"]
        elif source in ("config", "config1", "config2"):
            variants = [source]
        else:
            variants = ["config"]
        info = None
        for variant in variants:
            xml_text = self._cgi_client.get_motion_detect_config(variant)
            if not xml_text:
                continue
            parsed = parse_motion_detect_zones(xml_text)
            if parsed.zones or parsed.grid:
                info = parsed
                break
            if info is None:
                info = parsed
        self._md_info = info
        self._zones_overlay_cache.invalidate()
        if info:
            self._auto_zoom.md_enabled = info.enabled
        if s.privacy_mask_overlay:
            mask_xml = self._cgi_client.get_osd_mask_area()
            self._privacy_masks = parse_osd_mask_areas(mask_xml or "")
        else:
            self._privacy_masks = []

    def _probe_camera_motion_caps(self) -> None:
        try:
            has_ptz, has_optical = self._cgi_client.probe_ptz_capabilities()
        except Exception:
            has_ptz, has_optical = False, False
        self._auto_zoom.has_ptz = has_ptz
        self._auto_zoom.has_optical_zoom = has_optical
        az = self.motion_settings.auto_zoom
        if has_ptz and az.ptz_speed is not None:
            try:
                self._cgi_client.set_ptz_speed(int(az.ptz_speed))
            except Exception:
                pass
        self.root.after(0, lambda: self.ui.set_motion_ptz_caps(has_ptz, has_optical))

    def _test_ptz_capabilities(self) -> None:
        def worker():
            try:
                self._cgi_client.ptz_move("Right")
                time.sleep(0.2)
                self._cgi_client.ptz_stop()
                self._cgi_client.zoom_in()
                time.sleep(0.25)
                self._cgi_client.zoom_stop()
                self._cgi_client.zoom_out()
                time.sleep(0.25)
                self._cgi_client.zoom_stop()
            except Exception:
                pass
            self._probe_camera_motion_caps()
        threading.Thread(target=worker, daemon=True).start()

    def _save_motion_snapshot(self, frame) -> None:
        snap = self.motion_settings.snapshot_on_motion
        if not snap.enabled or frame is None:
            return
        try:
            dest = Path(snap.dir).expanduser()
            dest.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = dest / f"motion_{self.camera_ip}_{ts}.jpg"
            cv2.imwrite(str(path), frame)
        except Exception:
            pass

    def _start_auto_zoom_tick(self) -> None:
        self.root.after(200, self._auto_zoom_tick)

    def _auto_zoom_tick(self) -> None:
        if not self._ui_active:
            return
        try:
            frame = self.current_frame
            if frame is None:
                pass
            else:
                fh, fw = frame.shape[:2]
                gate_open = True
                if self.motion_settings.require_audio_gate:
                    gate_open = bool(self._gate_is_open(for_playback=False))
                action = self._auto_zoom.tick(
                    self._motion_level, self._motion_boxes, (fw, fh), gate_open,
                )
                for cmd, params in action.ptz_commands:
                    self._ptz_cgi(cmd, params)
                self._digital_crop = action.digital_crop
                if action.snapshot and frame is not None:
                    self._save_motion_snapshot(frame)
                if self.motion_settings.auto_zoom.show_state_badge:
                    self.ui.set_motion_zoom_state(action.state_label)
                    self.ui.indicator_strip.set_zoom_state(action.state_label)
        except Exception:
            pass
        if self._ui_active:
            try:
                self.root.after(100, self._auto_zoom_tick)
            except tk.TclError:
                pass

    def _compute_motion_level(self, frame):
        level, boxes = self._motion_analyzer.analyze_with_zones(frame, self._md_info)
        self._motion_ema = level
        self._motion_boxes = boxes
        if self.motion_settings.heatmap.enabled and frame is not None:
            self._heatmap.update(frame.shape, boxes)
        return level

    def _motion_analysis_needed(self) -> bool:
        s = self.motion_settings
        return motion_analysis_needed(
            show_live_overlay=s.show_live_overlay,
            auto_zoom_enabled=s.auto_zoom.enabled,
            heatmap_enabled=s.heatmap.enabled,
            snapshot_enabled=s.snapshot_on_motion.enabled,
            debug_fsm_overlay=s.debug_fsm_overlay,
        )

    def _maybe_update_motion(self, frame) -> None:
        self._motion_display_frame_index += 1
        now = time.monotonic()
        if not should_run_motion_analysis(
            needed=self._motion_analysis_needed(),
            frame_index=self._motion_display_frame_index,
            last_analysis_monotonic=self._last_motion_analysis_monotonic,
            now_monotonic=now,
        ):
            return
        self._last_motion_analysis_monotonic = now
        self._motion_level = self._compute_motion_level(frame)

    def _visual_overlays_needed(self) -> bool:
        s = self.motion_settings
        return visual_overlays_needed(
            show_live_overlay=s.show_live_overlay,
            show_configured_zones=s.show_configured_zones,
            privacy_mask_overlay=s.privacy_mask_overlay,
            has_privacy_masks=bool(self._privacy_masks),
            flash_alarm_border=s.flash_alarm_border,
            alarm_active=bool(self._camera_alarm_active),
            debug_fsm_overlay=s.debug_fsm_overlay,
        )

    def _get_label_display_size(self, frame) -> tuple:
        """Solo usa estado cacheado; sin llamadas Tk (puede ejecutarse en hilo prep)."""
        if self._display_size and self._display_size[0] > 1 and self._display_size[1] > 1:
            return self._display_size
        h, w = frame.shape[:2]
        return (max(1, w), max(1, h))

    def _prepare_display_frame(self, frame) -> Optional[_PreparedDisplay]:
        if frame is None:
            return None
        display_width, display_height = self._get_label_display_size(frame)
        height, width = frame.shape[:2]
        if display_width > 1 and display_height > 1:
            scale = min(display_width / width, display_height / height)
            new_width = max(1, int(width * scale))
            new_height = max(1, int(height * scale))
            process_w, process_h = new_width, new_height
            interp = cv2.INTER_AREA if process_w < width else cv2.INTER_LINEAR
            frame_resized = cv2.resize(
                frame, (process_w, process_h), interpolation=interp,
            )
            zoom_x = zoom_y = 1
        else:
            frame_resized = frame
            new_width, new_height = frame.shape[1], frame.shape[0]
            process_w, process_h = new_width, new_height
            zoom_x = zoom_y = 1

        self.current_frame = frame
        self._maybe_update_motion(frame)
        src_h, src_w = frame.shape[:2]

        display_frame = frame_resized
        if getattr(self, "_digital_crop", None) is not None:
            crop = self._digital_crop
            sh, sw = frame.shape[:2]
            x1, y1, x2, y2 = crop
            scale_x = process_w / max(1, sw)
            scale_y = process_h / max(1, sh)
            cx1 = int(x1 * scale_x)
            cy1 = int(y1 * scale_y)
            cx2 = max(cx1 + 2, int(x2 * scale_x))
            cy2 = max(cy1 + 2, int(y2 * scale_y))
            cropped = display_frame[cy1:cy2, cx1:cx2]
            if cropped.size > 0:
                display_frame = cv2.resize(
                    cropped, (process_w, process_h), interpolation=cv2.INTER_LINEAR,
                )

        if self.motion_settings.heatmap.enabled:
            display_frame = self._heatmap.overlay(display_frame)

        self._prep_frame_index += 1
        draw_overlays = (
            self._visual_overlays_needed()
            and (self._prep_frame_index % OVERLAY_EVERY_N_FRAMES == 0)
        )
        if draw_overlays:
            now = time.time()
            if self.motion_settings.flash_alarm_border and self._camera_alarm_active:
                if now - self._alarm_flash_ts > 0.5:
                    self._alarm_flash_on = not self._alarm_flash_on
                    self._alarm_flash_ts = now
            else:
                self._alarm_flash_on = False

            debug_text = None
            if self.motion_settings.debug_fsm_overlay:
                az = self._auto_zoom
                debug_text = (
                    f"{az.state.value} | {self._motion_level:.0f}% | "
                    f"boxes={len(self._motion_boxes)} | "
                    f"ptz={az.has_ptz} opt={az.has_optical_zoom}"
                )

            display_frame = draw_motion_overlays(
                display_frame,
                self.motion_settings,
                self._motion_boxes,
                self._md_info,
                self._privacy_masks if self.motion_settings.privacy_mask_overlay else None,
                self._motion_level,
                (src_w, src_h),
                (process_w, process_h),
                debug_text=debug_text,
                alarm_flash=self._alarm_flash_on,
                zones_cache=self._zones_overlay_cache,
            )

        h, w = display_frame.shape[:2]
        if (
            self._rgb_buffer_a is None
            or self._rgb_buffer_a.shape[0] != h
            or self._rgb_buffer_a.shape[1] != w
        ):
            self._rgb_buffer_a = np.empty((h, w, 3), dtype=np.uint8)
            self._rgb_buffer_b = np.empty((h, w, 3), dtype=np.uint8)
        buf = self._rgb_buffer_a if self._rgb_ping else self._rgb_buffer_b
        self._rgb_ping = not self._rgb_ping
        cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB, dst=buf)
        return _PreparedDisplay(
            rgb=buf,
            stream_w=width,
            stream_h=height,
            display_label_w=display_width,
            display_label_h=display_height,
            zoom_x=zoom_x,
            zoom_y=zoom_y,
        )

    def _rgb_to_photoimage(self, rgb_array, existing=None, zoom_x=1, zoom_y=1):
        try:
            h, w = rgb_array.shape[:2]
            if np is not None and not rgb_array.flags["C_CONTIGUOUS"]:
                rgb_array = np.ascontiguousarray(rgb_array)
            header = f"P6 {w} {h} 255 ".encode("ascii")
            data = header + rgb_array.tobytes()
            if existing is not None and self._photo_size == (w, h):
                try:
                    self.root.tk.call(existing.name, "put", data, "-format", "ppm", "-to", 0, 0)
                    return existing
                except tk.TclError:
                    pass
            photo = tk.PhotoImage(master=self.root, data=data, format="PPM")
            self._photo_size = (w, h)
            return photo
        except tk.TclError:
            image = Image.fromarray(rgb_array)
            photo = ImageTk.PhotoImage(image=image, master=self.root)
            self._photo_size = (rgb_array.shape[1], rgb_array.shape[0])
            return photo

    def _tick_display_fps(self) -> None:
        now = time.monotonic()
        self._display_fps_window_count += 1
        elapsed = now - self._display_fps_window_start
        if elapsed >= 0.5:
            self._display_fps = self._display_fps_window_count / elapsed
            self._display_fps_window_count = 0
            self._display_fps_window_start = now
            if self.is_streaming:
                print(
                    f"[foscam] display_fps={self._display_fps:.1f}",
                    file=sys.stderr,
                    flush=True,
                )
        if self.is_streaming and self._display_fps > 0:
            self.ui.set_display_fps(self._display_fps, self._decode_fps)

    def _schedule_next_display(self, painted: bool) -> None:
        if not self._ui_active:
            return
        elapsed_ms = 0.0
        if painted:
            elapsed_ms = (time.monotonic() - self._last_paint_monotonic) * 1000.0
        depth = self.display_queue.qsize()
        if not painted:
            depth = max(depth, self.frame_queue.qsize())
        delay = next_display_delay_ms(depth, elapsed_ms)
        try:
            self.root.after(delay, self._update_display_loop)
        except tk.TclError:
            pass

    def _display_prep_loop(self) -> None:
        while not self._display_prep_stop.is_set():
            if not self.is_streaming:
                time.sleep(0.05)
                continue
            try:
                frame = self.frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            while True:
                try:
                    frame = self.frame_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                prepared = self._prepare_display_frame(frame)
                if prepared is None:
                    continue
                try:
                    self.display_queue.put_nowait(prepared)
                except queue.Full:
                    try:
                        self.display_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.display_queue.put_nowait(prepared)
                    except queue.Full:
                        pass
            except Exception:
                pass

    def _meter_ui_loop(self):
        try:
            level_db = self._audio_level_db_ema
            gate_db = self.audio_gate_db
            above = level_db >= gate_db if gate_db > GATE_SLIDER_MIN + 1 else True
            hint = "≥ umbral" if above else "< umbral"
            level_text = f"{level_db:.1f} dB {hint} ({gate_db:.0f} dB)"

            self.vu_meter.redraw_db(level_db)
            self.vu_meter.set_gate_db(gate_db)
            self._audio_level_label_var.set(level_text)

            strip = self.ui.indicator_strip
            strip.vu_meter.redraw_db(level_db)
            strip.vu_meter.set_gate_db(gate_db)
            strip._audio_level_label_var.set(level_text)

            mini = getattr(self.ui, "sidebar_mini_vu", None)
            if mini is not None:
                mini.redraw_db(level_db)
                mini.set_gate_db(gate_db)
                self.ui._sidebar_mini_db_var.set(f"{level_db:.0f} dB")

            if self._uses_live_audio_gate():
                self.ui.set_gate_state(self._gate_open_state)
            else:
                self.ui.set_gate_state(self._gate_is_open(for_playback=False))

            motion_ratio = self._motion_level / 100.0
            self._motion_meter.set(motion_ratio)
            strip._motion_meter.set(motion_ratio)
            self.ui._sidebar_mini_motion.set(motion_ratio)

            if self._camera_alarm_active is None:
                self._camera_alarm_meter.set(0)
                self._camera_alarm_label_var.set("N/D")
                strip._camera_alarm_meter.set(0)
                strip._camera_alarm_label_var.set("N/D")
                self.ui.set_alarm_badge(None)
            elif self._camera_alarm_active:
                self._camera_alarm_meter.set(1.0)
                self._camera_alarm_label_var.set("Activa")
                strip._camera_alarm_meter.set(1.0)
                strip._camera_alarm_label_var.set("Activa")
                self.ui.set_alarm_badge(True)
            else:
                self._camera_alarm_meter.set(0)
                self._camera_alarm_label_var.set("Inactiva")
                strip._camera_alarm_meter.set(0)
                strip._camera_alarm_label_var.set("Inactiva")
                self.ui.set_alarm_badge(False)
        except tk.TclError:
            return
        if self._ui_active:
            try:
                self.root.after(100, self._meter_ui_loop)
            except tk.TclError:
                pass

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
                self._refresh_motion_zones(force=False)
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
        self.root.bind("h", lambda e: self._toggle_hud())
        self.root.bind("H", lambda e: self._toggle_hud())
        self.root.bind("[", lambda e: self._toggle_sidebar_collapsed())
        self.root.bind("]", lambda e: self._toggle_sidebar_collapsed())
        self.root.bind("m", lambda e: self._toggle_motion_shortcut("show_live_overlay"))
        self.root.bind("M", lambda e: self._toggle_motion_shortcut("show_live_overlay"))
        self.root.bind("v", lambda e: self._toggle_motion_shortcut("show_configured_zones"))
        self.root.bind("V", lambda e: self._toggle_motion_shortcut("show_configured_zones"))
        self.root.bind("Z", lambda e: self._toggle_motion_shortcut("auto_zoom.enabled"))
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
    
    def _toggle_motion_shortcut(self, key: str) -> None:
        if key == "auto_zoom.enabled":
            val = not self.motion_settings.auto_zoom.enabled
        elif key == "show_live_overlay":
            val = not self.motion_settings.show_live_overlay
        elif key == "show_configured_zones":
            val = not self.motion_settings.show_configured_zones
        else:
            return
        self._on_motion_setting(key, val)

    def _ptz_move(self, cmd):
        """Start PTZ movement (arrow key pressed). Only one move command per press (ignore key repeat)."""
        self._auto_zoom.notify_manual_ptz()
        if self._ptz_direction is not None:
            return  # Already moving in some direction, ignore repeat
        self._ptz_direction = cmd
        self._ptz_cgi(cmd)
    
    def _ptz_stop(self):
        """Stop PTZ movement (arrow key released)."""
        self._ptz_direction = None
        self._ptz_cgi("ptzStopRun")

    def _on_any_keypress(self, event):
        """Tecla 0: ptzReset. a/s: volumen. Z: zoom auto (bind directo)."""
        if event.keysym in ("0", "KP_0") or getattr(event, "keycode", None) == 96:
            self._ptz_reset()
        elif event.keysym in ("a", "A"):
            self._volume_change(1)
        elif event.keysym in ("s", "S"):
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
        msg = f"Conectando a {self.camera_ip}:{self.camera_port}…"
        if self.use_nvidia_decode:
            msg += f" (NVIDIA {self.nvidia_source})"
        self._set_status_short(msg)
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
                            self._audio_ffplay_fallback = False
                            self._decode_backend = "ffmpeg"
                            self.root.after(0, self._update_ui_connected)
                            self._fetch_and_set_camera_name()
                            self.root.after(0, lambda v=self._playback_volume: self.volume_var.set(f"Vol: {v}"))
                            if audio_stream is not None:
                                sr = int(audio_stream.sample_rate)
                                ch = _stream_channel_count(audio_stream)
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
                            live_audio = self._start_pyav_audio_sidecar_with_retry(rtsp_url)
                            self._audio_ffplay_fallback = not live_audio
                            if not live_audio:
                                if PYAV_AUDIO_AVAILABLE:
                                    err = self._last_sidecar_error or "desconocido"
                                    self.root.after(0, lambda e=err: self._set_status_short(
                                        f"Audio: ffplay (sidecar falló: {e})",
                                    ))
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
        if not self.is_streaming or self._shutting_down:
            outdata.fill(0)
            return
        q = getattr(self, "_audio_queue", None)
        if q is None:
            outdata.fill(0)
            return
        vol_pct = max(0, min(100, getattr(self, "_playback_volume", 50)))
        need = frames * (getattr(self, "_sd_channels", 1))
        out = outdata.ravel()
        filled = 0
        while filled < need and q is not None:
            try:
                chunk = q.get_nowait()
                if chunk is None:
                    break
                if np is not None:
                    processed, gate_open = process_playback_chunk(
                        chunk, self.audio_gate_db, vol_pct,
                    )
                    self._gate_open_state = gate_open
                    self._log_gate_debug(self._samples_db(chunk), gate_open)
                    samples = processed
                else:
                    samples = self._apply_audio_gate(chunk) * (vol_pct / 100.0)
                n = min(len(samples), need - filled)
                out[filled:filled + n] = samples[:n]
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
                            ch = arr.shape[1] if arr.ndim > 1 else 1
                            step = 1024 * ch
                            for i in range(0, arr.size, step):
                                chunk = arr.ravel()[i:i + step].copy()
                                if len(chunk) > 0:
                                    self._update_audio_level_from_samples(chunk)
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
        """Reproduce audio RTSP con ffplay (-volume; puerta vía filtro agate)."""
        if (
            not AUDIO_AVAILABLE
            or not self.rtsp_url
            or self._audio_stop.is_set()
            or not self.is_streaming
            or self._shutting_down
        ):
            return
        try:
            vol = max(0, min(100, getattr(self, "_playback_volume", 50)))
            cmd = ["ffplay", "-nodisp", "-autoexit", "-volume", str(vol), "-i", self.rtsp_url]
            af = self._ffplay_audio_filter()
            if af:
                cmd = ["ffplay", "-nodisp", "-autoexit", "-volume", str(vol), "-af", af, "-i", self.rtsp_url]
            self._last_ffplay_cmd = " ".join(cmd)
            if self._audio_gate_debug:
                print(f"[ffplay] start pid-pending cmd={self._last_ffplay_cmd}", file=sys.stderr)
            self.root.after(0, self._update_technical_details)
            # start_new_session=True para poder matar el grupo al cerrar (evita que ffplay quede abierto)
            self._audio_process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            proc = self._audio_process  # referencia local: otro hilo puede poner _audio_process = None
            if self._audio_gate_debug and proc is not None:
                print(f"[ffplay] pid={proc.pid}", file=sys.stderr)
            while (
                proc is not None
                and proc.poll() is None
                and not self._audio_stop.is_set()
                and self.is_streaming
                and not self._shutting_down
            ):
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
    
    def _tick_decode_fps(self) -> None:
        now = time.monotonic()
        self._decode_fps_window_count += 1
        elapsed = now - self._decode_fps_window_start
        if elapsed >= 0.5:
            self._decode_fps = self._decode_fps_window_count / elapsed
            self._decode_fps_window_count = 0
            self._decode_fps_window_start = now
            if self.is_streaming:
                print(
                    f"[foscam] decode_fps={self._decode_fps:.1f}",
                    file=sys.stderr,
                    flush=True,
                )

    def _video_reader_thread(self):
        """Thread to continuously read frames from RTSP stream."""
        while self.is_streaming and self.cap is not None:
            try:
                ret, frame = self.cap.read()
                if ret and frame is not None:
                    self._tick_decode_fps()
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
            except Exception as e:
                if self.is_streaming:
                    self.root.after(0, lambda: self._set_connection_state("reconnecting"))
                    self.root.after(0, lambda err=str(e): self._set_status_short(f"Stream error: {err}"))
                time.sleep(1)
                if not self.is_streaming:
                    break
    
    def _on_display_resize(self, event):
        """When window/display area is resized, use new size for scaling only (no automatic resolution change)."""
        label = getattr(self, "video_label", None)
        if label is None or event.widget != label or event.width < 2 or event.height < 2:
            return
        self._display_size = (event.width, event.height)

    def _refresh_display_size_from_label(self) -> None:
        """Actualiza _display_size desde Tk (solo hilo principal)."""
        label = getattr(self, "video_label", None)
        if label is None:
            return
        w, h = label.winfo_width(), label.winfo_height()
        if w > 1 and h > 1:
            self._display_size = (w, h)

    def _update_display_loop(self):
        """Pinta el último frame preparado; el trabajo pesado va en _display_prep_loop."""
        self._refresh_display_size_from_label()
        painted = False
        try:
            prepared = None
            while True:
                try:
                    prepared = self.display_queue.get_nowait()
                except queue.Empty:
                    break

            if prepared is not None:
                prev = self._video_photo
                size = (prepared.rgb.shape[1], prepared.rgb.shape[0])
                self._video_photo = self._rgb_to_photoimage(
                    prepared.rgb, prev, prepared.zoom_x, prepared.zoom_y,
                )
                self._last_image_size = size
                if prev is not self._video_photo:
                    self.video_label.configure(image=self._video_photo, text="")
                self.video_label.image = self._video_photo

                self.resolution_var.set(f"{prepared.stream_w}×{prepared.stream_h}")
                if prepared.display_label_w > 1 and prepared.display_label_h > 1:
                    self.display_size_var.set(
                        f"Display {prepared.display_label_w}×{prepared.display_label_h}",
                    )
                self._display_frame_count += 1
                self._tick_display_fps()
                self._last_paint_monotonic = time.monotonic()
                painted = True
                if self._display_frame_count % DISPLAY_DETAIL_EVERY_N == 0:
                    self._update_technical_details()

        except Exception as e:
            if self.is_streaming and self._ui_active:
                self._set_status_short(f"Display error: {str(e)}")

        self._schedule_next_display(painted)
    
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
        self.root.after(2000, self._close_after_connection_failed)

    def _close_after_connection_failed(self):
        self._shutting_down = True
        self._ui_active = False
        self._cancel_pending_after(all_callbacks=True)
        self._disconnect_camera()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _cancel_pending_after(self, all_callbacks: bool = False) -> None:
        """Cancela reinicios de ffplay y, al cerrar, también guardados diferidos."""
        for attr in ("_gate_restart_after_id",):
            aid = getattr(self, attr, None)
            if aid is not None:
                setattr(self, attr, None)
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass
        if not all_callbacks:
            return
        for attr in ("_prefs_save_after_id", "_geom_save_after_id"):
            aid = getattr(self, attr, None)
            if aid is not None:
                setattr(self, attr, None)
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass

    def _stop_sounddevice_stream(self) -> None:
        stream = getattr(self, "_sd_stream", None)
        if stream is None:
            return
        self._sd_stream = None
        try:
            if hasattr(stream, "abort"):
                stream.abort()
        except Exception:
            pass
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    def _terminate_audio_process(self):
        """Cierra el proceso de audio (ffplay); mata el grupo de procesos si hace falta."""
        if self._audio_process is None:
            return
        proc = self._audio_process
        self._audio_process = None
        if proc.poll() is not None:
            return
        pid = proc.pid

        def _wait_proc(timeout: float) -> None:
            try:
                proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                pass

        try:
            if hasattr(os, "killpg"):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    _wait_proc(0.8)
                    if proc.poll() is None:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                        _wait_proc(0.5)
                    return
                except (ProcessLookupError, OSError):
                    pass
            proc.terminate()
            _wait_proc(1.0)
            if proc.poll() is None:
                proc.kill()
                _wait_proc(0.5)
        except (ProcessLookupError, OSError):
            pass
        except Exception:
            pass

    def _disconnect_camera(self):
        """Disconnect from the camera and stop streaming."""
        self.is_streaming = False
        self._audio_ffplay_fallback = False
        self._gate_open_state = None
        self._audio_stop.set()
        self._cancel_pending_after()
        self._audio_restart_in_progress = False
        self._cgi_poll_stop.set()
        self._stop_audio_level_meter()
        self._prev_gray = None
        self._motion_analyzer.reset()
        self._auto_zoom.reset()
        self._heatmap.reset()
        self._zones_overlay_cache.invalidate()
        self._digital_crop = None
        self._motion_level = 0.0
        self._motion_ema = 0.0
        self._audio_level = 0.0
        self._audio_level_db = AUDIO_DB_FLOOR
        self._audio_level_db_ema = float(AUDIO_DB_FLOOR)
        self._camera_alarm_active = None
        self.volume_var.set("Vol: --")
        if getattr(self, "_use_pyav", False):
            self._use_pyav = False
            self._audio_queue = None
            container = self._av_container
            if container is not None:
                self._av_container = None
                try:
                    container.close()
                except Exception:
                    pass
            self._stop_sounddevice_stream()
            if self._av_demux_thread is not None and self._av_demux_thread.is_alive():
                self._av_demux_thread.join(timeout=2.0)
            self._av_demux_thread = None
        else:
            if getattr(self, "_use_pyav_audio_sidecar", False):
                self._stop_pyav_audio_sidecar()
            self._terminate_audio_process()
            if self.audio_thread and self.audio_thread.is_alive():
                self.audio_thread.join(timeout=3.0)
            if self.video_thread and self.video_thread.is_alive():
                self.video_thread.join(timeout=2.0)
            if self.cap is not None:
                self.cap.release()
                self.cap = None
        
        # Clear frame queues
        for q in (self.frame_queue, self.display_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._display_fps = 0.0
        self._display_fps_window_count = 0
        self._motion_display_frame_index = 0
        self._prep_frame_index = 0
        self.ui.set_display_fps(None)

        self.current_frame = None
        self.video_label.configure(image="", text="Desconectado")
        self.video_label.image = None
        self._video_photo = None
        self._photo_size = None
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
        if self._close_started:
            return
        self._close_started = True
        self._shutting_down = True
        self._ui_active = False
        self._display_prep_stop.set()
        self._cancel_pending_after(all_callbacks=True)
        try:
            self._save_window_geometry()
            self._do_save_prefs()
        except tk.TclError:
            pass
        self._disconnect_camera()
        try:
            self.ui.destroy_overlays()
        except (tk.TclError, AttributeError):
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


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
    
    nvidia_group = parser.add_mutually_exclusive_group()
    nvidia_group.add_argument(
        '--nvidia',
        action='store_true',
        help='Forzar decodificación GPU NVIDIA (GStreamer nvh264dec).',
    )
    nvidia_group.add_argument(
        '--no-nvidia',
        action='store_true',
        help='Forzar vídeo por CPU (sin GPU).',
    )

    parser.add_argument(
        '--audio-gate-debug',
        action='store_true',
        help='Log de diagnóstico de puerta de ruido en stderr (o FOSCAM_AUDIO_GATE_DEBUG=1).',
    )

    parser.add_argument(
        '--ui-scale',
        type=float,
        default=None,
        metavar='FACTOR',
        help=(
            'Escala de la interfaz CustomTkinter (default: valor en viewer.json o 1.5). '
            'Ej: 1.5, 2.0, 2.5. Afecta controles y tamaño sugerido de ventana.'
        ),
    )

    motion_group = parser.add_argument_group('movimiento y zoom')
    motion_group.add_argument('--motion-live-overlay', action='store_true', help='Activar overlay de movimiento en vivo')
    motion_group.add_argument('--no-motion-live-overlay', action='store_true', help='Desactivar overlay de movimiento')
    motion_group.add_argument('--motion-zones-overlay', action='store_true', help='Mostrar zonas MD de la cámara')
    motion_group.add_argument('--no-motion-zones-overlay', action='store_true', help='Ocultar zonas MD')
    motion_group.add_argument('--auto-zoom', action='store_true', help='Activar zoom automático al movimiento')
    motion_group.add_argument('--no-auto-zoom', action='store_true', help='Desactivar zoom automático')
    motion_group.add_argument('--motion-sensitivity', type=float, default=None, metavar='0-100')
    motion_group.add_argument('--auto-zoom-return-sec', type=float, default=None, metavar='SEC')
    motion_group.add_argument(
        '--auto-zoom-mode',
        choices=['auto', 'digital', 'ptz', 'ptz_pan_digital_zoom'],
        default=None,
    )
    motion_group.add_argument('--motion-profile', type=str, default=None, metavar='NAME')
    motion_group.add_argument('--motion-config', type=str, default=None, metavar='PATH',
                              help='JSON parcial con claves motion para merge al arrancar')
    
    args = parser.parse_args()
    
    # Validate IP address
    if not args.ip or not args.ip.strip():
        print("Error: IP address is required", file=sys.stderr)
        sys.exit(1)
    
    prefs = _load_viewer_prefs()
    ui_scale = getattr(args, "ui_scale", None)
    if ui_scale is None:
        ui_scale = prefs.get("ui_scale", ui_theme.DEFAULT_UI_SCALE)
    if args.nvidia:
        use_nvidia = True
        nvidia_source = "manual"
    elif getattr(args, "no_nvidia", False):
        use_nvidia = False
        nvidia_source = "off"
    elif probe_nvh264dec_available():
        use_nvidia = True
        nvidia_source = "auto"
    else:
        use_nvidia = False
        nvidia_source = "off"

    motion_cli = {}
    if args.motion_live_overlay:
        motion_cli["show_live_overlay"] = True
    if args.no_motion_live_overlay:
        motion_cli["show_live_overlay"] = False
    if args.motion_zones_overlay:
        motion_cli["show_configured_zones"] = True
    if args.no_motion_zones_overlay:
        motion_cli["show_configured_zones"] = False
    if args.auto_zoom:
        motion_cli.setdefault("auto_zoom", {})["enabled"] = True
    if args.no_auto_zoom:
        motion_cli.setdefault("auto_zoom", {})["enabled"] = False
    if args.motion_sensitivity is not None:
        motion_cli["sensitivity"] = float(args.motion_sensitivity)
    if args.auto_zoom_return_sec is not None:
        motion_cli.setdefault("auto_zoom", {})["return_sec"] = float(args.auto_zoom_return_sec)
    if args.auto_zoom_mode:
        motion_cli.setdefault("auto_zoom", {})["mode"] = args.auto_zoom_mode
    if args.motion_profile:
        motion_cli["active_profile"] = args.motion_profile
    if args.motion_config:
        try:
            with open(args.motion_config, encoding="utf-8") as f:
                extra = json.load(f)
            if isinstance(extra, dict):
                from foscam.motion import _merge_dict
                motion_cli = _merge_dict(motion_cli, extra.get("motion", extra))
        except (OSError, json.JSONDecodeError, TypeError) as e:
            print(f"Error leyendo --motion-config: {e}", file=sys.stderr)
            sys.exit(1)

    ui_theme.apply_theme(float(ui_scale))
    root = ctk.CTk()
    app = FoscamViewer(
        root, args.ip.strip(), args.port, args.user, args.password,
        use_sub_stream=args.sub,
        audio_gate_db=args.audio_gate_db,
        use_nvidia_decode=use_nvidia,
        nvidia_source=nvidia_source,
        ui_scale=ui_scale,
        audio_gate_debug=getattr(args, "audio_gate_debug", False),
        motion_cli_overrides=motion_cli or None,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
