"""Tema oscuro teal para el visor."""

from importlib.resources import as_file, files
from pathlib import Path
from typing import Optional, Tuple

import customtkinter as ctk

DEFAULT_UI_SCALE = 2.0
_UI_SCALE = DEFAULT_UI_SCALE

SIDEBAR_WIDTH_BASE = 300
AUDIO_METER_CANVAS_W_BASE = 260
AUDIO_METER_CANVAS_H_BASE = 16

BG_APP = "#0f1117"
BG_VIDEO = "#08090c"
BG_CARD = "#181b24"
BG_ELEVATED = "#22262f"
ACCENT = "#2ec4b6"
ACCENT_HOVER = "#3dd6c8"
ACCENT_MUTED = "#1a6f68"
TEXT = "#e6e8ec"
TEXT_MUTED = "#8b919c"
SUCCESS = "#4ade80"
WARNING = "#fbbf24"
DANGER = "#f87171"
VU_TRACK = "#22262f"
VU_FILL = "#2ec4b6"


def get_ui_scale() -> float:
    return _UI_SCALE


def sidebar_width() -> int:
    return int(SIDEBAR_WIDTH_BASE * _UI_SCALE)


def audio_meter_size() -> Tuple[int, int]:
    return (
        int(AUDIO_METER_CANVAS_W_BASE * _UI_SCALE),
        int(AUDIO_METER_CANVAS_H_BASE * _UI_SCALE),
    )


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
