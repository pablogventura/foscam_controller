"""Tema oscuro teal para el visor."""

from importlib.resources import as_file, files
from pathlib import Path
from typing import Optional, Tuple

import customtkinter as ctk

DEFAULT_UI_SCALE = 1.5
_UI_SCALE = DEFAULT_UI_SCALE

SIDEBAR_WIDTH_BASE = 300
SIDEBAR_COLLAPSED_WIDTH_BASE = 56
AUDIO_METER_CANVAS_W_BASE = 260
AUDIO_METER_CANVAS_H_BASE = 16
INDICATOR_STRIP_METER_W_BASE = 200

BG_APP = "#0f1117"
BG_VIDEO = "#000000"
BG_CARD = "#141820"
BG_ELEVATED = "#1c2230"
CHROME_BG = "#222a38"
CHROME_BTN_BG = "#2d3648"
CHROME_BTN_BORDER = "#5a6478"
CHROME_BTN_HOVER = "#3d4a62"
OVERLAY_BG = "#1e2636"
OVERLAY_BORDER = "#5a6478"
OVERLAY_HOVER = CHROME_BTN_HOVER

ACCENT = "#26b5a8"
ACCENT_HOVER = "#32c9bb"
ACCENT_MUTED = "#1a6f68"
ACCENT_SOFT = "#1e8a80"

TEXT = "#f0f2f5"
TEXT_MUTED = "#b8c0d0"
TEXT_GHOST = "#3a4254"
TEXT_GHOST_MUTED = "#2a3040"

STATE_LIVE = "#4ade80"
STATE_ALARM = "#fbbf24"
STATE_OK = "#5c6573"
STATE_MUTED = "#f87171"
GATE_OPEN = ACCENT
GATE_CLOSED = "#5c6573"

SUCCESS = STATE_LIVE
WARNING = STATE_ALARM
DANGER = STATE_MUTED

VU_TRACK = "#1c2230"
VU_FILL = ACCENT


def get_ui_scale() -> float:
    return _UI_SCALE


def sidebar_width() -> int:
    return int(SIDEBAR_WIDTH_BASE * _UI_SCALE)


def sidebar_collapsed_width() -> int:
    return int(SIDEBAR_COLLAPSED_WIDTH_BASE * _UI_SCALE)


def audio_meter_size() -> Tuple[int, int]:
    return (
        int(AUDIO_METER_CANVAS_W_BASE * _UI_SCALE),
        int(AUDIO_METER_CANVAS_H_BASE * _UI_SCALE),
    )


def indicator_meter_size() -> Tuple[int, int]:
    return (
        int(INDICATOR_STRIP_METER_W_BASE * _UI_SCALE),
        int(AUDIO_METER_CANVAS_H_BASE * _UI_SCALE),
    )


def overlay_panel_kwargs(*, corner_radius: int = 12) -> dict:
    return {
        "fg_color": OVERLAY_BG,
        "border_width": 1,
        "border_color": OVERLAY_BORDER,
        "corner_radius": corner_radius,
    }


def chrome_panel_kwargs(*, corner_radius: int = 10) -> dict:
    """Panel legible sobre vídeo negro (toolbar, footer, pills)."""
    return {
        "fg_color": CHROME_BG,
        "border_width": 1,
        "border_color": CHROME_BTN_BORDER,
        "corner_radius": corner_radius,
    }


def chrome_button_kwargs(*, accent: bool = False, danger: bool = False) -> dict:
    """Botón con fondo sólido; evita negro sobre negro en el HUD."""
    if accent:
        border, text = ACCENT, ACCENT
    elif danger:
        border, text = DANGER, TEXT
    else:
        border, text = CHROME_BTN_BORDER, TEXT
    return {
        "fg_color": CHROME_BTN_BG,
        "border_width": 1,
        "border_color": border,
        "text_color": text,
        "hover_color": CHROME_BTN_HOVER,
    }


def overlay_alpha_idle() -> float:
    from foscam.ui.overlay import OVERLAY_IDLE_ALPHA
    return OVERLAY_IDLE_ALPHA


def overlay_alpha_hover() -> float:
    from foscam.ui.overlay import OVERLAY_HOVER_ALPHA
    return OVERLAY_HOVER_ALPHA


def glass_fg(_level: str = "normal") -> str:
    """CTk no soporta alpha real; devuelve hex glass premezclado."""
    return OVERLAY_BG


def default_window_geometry(scale: Optional[float] = None) -> str:
    s = float(scale if scale is not None else _UI_SCALE)
    return f"{int(1180 * s)}x{int(768 * s)}"


def theme_json_path() -> Path:
    ref = files("foscam.ui.assets").joinpath("theme_pool.json")
    with as_file(ref) as path:
        return Path(path)


def apply_theme(ui_scale: Optional[float] = None) -> float:
    global _UI_SCALE
    scale = float(ui_scale if ui_scale is not None else DEFAULT_UI_SCALE)
    scale = max(0.75, min(3.0, scale))
    _UI_SCALE = scale

    ctk.set_appearance_mode("dark")
    try:
        path = theme_json_path()
        if path.is_file():
            ctk.set_default_color_theme(str(path))
    except Exception:
        ctk.set_default_color_theme("dark-blue")
    try:
        ctk.set_widget_scaling(scale)
        ctk.set_window_scaling(scale)
    except Exception:
        pass
    return scale


def _font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(size=max(9, int(size * _UI_SCALE)), weight=weight)


def section_font():
    return _font(11, "bold")


def title_font():
    return _font(18, "bold")


def muted_font():
    return _font(11)


def small_font():
    return _font(10)
