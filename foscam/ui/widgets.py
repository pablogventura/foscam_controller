"""Widgets reutilizables del visor."""

from typing import Optional

import tkinter as tk

import customtkinter as ctk

from foscam.audio_gate import METER_DB_MAX, METER_DB_MIN, db_to_meter_ratio
from foscam.ui import theme as th


class GlassPanel(ctk.CTkFrame):
    """Panel HUD semitransparente (simulado con hex glass)."""

    def __init__(self, master, *, corner_radius: int = 12, **kwargs):
        panel = th.overlay_panel_kwargs(corner_radius=corner_radius)
        panel.update(kwargs)
        super().__init__(master, **panel)


class StatusPill(ctk.CTkFrame):
    """Indicador de estado de conexión."""

    _STYLES = {
        "connecting": (th.WARNING, "Conectando"),
        "live": (th.STATE_LIVE, "En vivo"),
        "error": (th.DANGER, "Error"),
        "offline": (th.TEXT_MUTED, "Desconectado"),
        "reconnecting": (th.WARNING, "Reconectando"),
    }

    def __init__(self, master, **kwargs):
        panel = th.chrome_panel_kwargs(corner_radius=12)
        panel.update(kwargs)
        super().__init__(master, **panel)
        self._dot = ctk.CTkLabel(self, text="●", width=16, font=ctk.CTkFont(size=14))
        self._dot.pack(side=tk.LEFT, padx=(10, 4), pady=6)
        self._label = ctk.CTkLabel(
            self, text="Conectando", font=th.small_font(), text_color=th.TEXT,
        )
        self._label.pack(side=tk.LEFT, padx=(0, 10), pady=6)
        self.set_state("connecting")

    def set_state(self, state: str) -> None:
        color, text = self._STYLES.get(state, self._STYLES["connecting"])
        self._dot.configure(text_color=color)
        self._label.configure(text=text, text_color=th.TEXT)


class SectionCard(ctk.CTkFrame):
    def __init__(self, master, title: str, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        ctk.CTkLabel(self, text=title, font=th.section_font(), text_color=th.ACCENT).pack(
            anchor=tk.W, padx=12, pady=(10, 6),
        )
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill=tk.X, padx=12, pady=(0, 12))


class VuMeter(ctk.CTkFrame):
    """Medidor de nivel audio con línea de umbral."""

    def __init__(self, master, width=None, height=None, **kwargs):
        if width is None or height is None:
            dw, dh = th.audio_meter_size()
            width = dw if width is None else width
            height = dh if height is None else height
        super().__init__(master, fg_color="transparent", **kwargs)
        self._meter_w = width
        self._meter_h = height
        self._gate_db = -38.0
        self._enabled = True
        self.canvas = ctk.CTkCanvas(
            self, width=width, height=height, bg="#1a1a1a",
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill=tk.X)

    def set_gate_db(self, db: float) -> None:
        self._gate_db = db

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def _db_to_x(self, db: float) -> int:
        ratio = db_to_meter_ratio(db, METER_DB_MIN, METER_DB_MAX)
        return int(ratio * (self._meter_w - 4)) + 2

    def redraw_db(self, level_db: float) -> None:
        """Barra y umbral en la misma escala dBFS (-60…0)."""
        c = self.canvas
        c.delete("all")
        if self._enabled:
            fill_w = max(0, self._db_to_x(level_db) - 1)
            if fill_w > 0:
                c.create_rectangle(
                    1, 1, 1 + fill_w, self._meter_h - 1,
                    fill=th.VU_FILL, outline="",
                )
        gx = self._db_to_x(self._gate_db)
        c.create_line(gx, 1, gx, self._meter_h - 1, fill=th.STATE_ALARM, width=2)

    def redraw(self, level_pct: float) -> None:
        """Compat: convierte porcentaje legacy a dB y delega."""
        ratio = max(0.0, min(1.0, float(level_pct) / 100.0))
        level_db = METER_DB_MIN + ratio * (METER_DB_MAX - METER_DB_MIN)
        self.redraw_db(level_db)


class ReconnectBanner(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        wrap = ctk.CTkFrame(
            self, fg_color="transparent", corner_radius=12,
            border_width=1, border_color=th.WARNING,
        )
        wrap.pack(padx=4, pady=4)
        ctk.CTkLabel(
            wrap, text="Reconectando stream…",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=th.WARNING,
        ).pack(padx=24, pady=16)


class PtzPad(ctk.CTkFrame):
    """Cruceta PTZ semitransparente."""

    def __init__(self, master, on_move, on_stop, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_move = on_move
        self._on_stop = on_stop
        cmds = {
            (0, 1): "ptzMoveUp",
            (2, 1): "ptzMoveDown",
            (1, 0): "ptzMoveLeft",
            (1, 2): "ptzMoveRight",
        }
        labels = {(0, 1): "▲", (2, 1): "▼", (1, 0): "◀", (1, 2): "▶"}
        for (r, c), cmd in cmds.items():
            btn = ctk.CTkButton(
                self, text=labels[(r, c)], width=36, height=32,
                **th.chrome_button_kwargs(),
                font=ctk.CTkFont(size=13),
            )
            btn.grid(row=r, column=c, padx=2, pady=2)
            btn.bind("<ButtonPress-1>", lambda e, command=cmd: self._press(command))
            btn.bind("<ButtonRelease-1>", lambda e: self._release())
        center = ctk.CTkLabel(self, text="PTZ", font=th.small_font(), text_color=th.TEXT_MUTED)
        center.grid(row=1, column=1, padx=2, pady=2)

    def _press(self, cmd: str) -> None:
        if self._on_move:
            self._on_move(cmd)

    def _release(self) -> None:
        if self._on_stop:
            self._on_stop()


class IndicatorStrip(ctk.CTkFrame):
    """Panel compacto de sensores para modo HUD minimal."""

    def __init__(self, master, on_toggle_hud=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        pad = int(10 * th.get_ui_scale())
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(padx=pad, pady=pad)

        header = ctk.CTkFrame(inner, fg_color="transparent")
        header.pack(fill=tk.X, pady=(0, 6))
        self.status_pill = StatusPill(header)
        self.status_pill.pack(side=tk.LEFT)
        self.hud_btn = ctk.CTkButton(
            header, text="H●", width=32, height=26,
            **th.chrome_button_kwargs(),
            font=th.small_font(),
            command=on_toggle_hud or (lambda: None),
        )
        self.hud_btn.pack(side=tk.RIGHT)

        ctk.CTkLabel(inner, text="Actividad", font=th.section_font(), text_color=th.ACCENT).pack(
            anchor=tk.W,
        )

        ctk.CTkLabel(inner, text="Movimiento", font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            anchor=tk.W, pady=(6, 0),
        )
        self._motion_meter = ctk.CTkProgressBar(inner, height=8)
        self._motion_meter.pack(fill=tk.X, pady=(2, 6))
        self._motion_meter.set(0)

        ctk.CTkLabel(inner, text="Audio", font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            anchor=tk.W,
        )
        mw, mh = th.indicator_meter_size()
        self.vu_meter = VuMeter(inner, width=mw, height=mh)
        self.vu_meter.pack(fill=tk.X, pady=(2, 2))

        self._audio_level_label_var = tk.StringVar(value="— dB")
        ctk.CTkLabel(
            inner, textvariable=self._audio_level_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 6))

        gate_row = ctk.CTkFrame(inner, fg_color="transparent")
        gate_row.pack(fill=tk.X, pady=(0, 4))
        ctk.CTkLabel(gate_row, text="Puerta", font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            side=tk.LEFT,
        )
        self._gate_state_var = tk.StringVar(value="N/D")
        self.gate_state_badge = ctk.CTkLabel(
            gate_row, textvariable=self._gate_state_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
            fg_color=th.CHROME_BTN_BG, corner_radius=6,
        )
        self.gate_state_badge.pack(side=tk.RIGHT)

        alarm_row = ctk.CTkFrame(inner, fg_color="transparent")
        alarm_row.pack(fill=tk.X, pady=(2, 0))
        ctk.CTkLabel(alarm_row, text="Alarma", font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            side=tk.LEFT,
        )
        self._camera_alarm_label_var = tk.StringVar(value="N/D")
        self.alarm_badge = ctk.CTkLabel(
            alarm_row, textvariable=self._camera_alarm_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
            fg_color=th.CHROME_BTN_BG, corner_radius=6,
        )
        self.alarm_badge.pack(side=tk.RIGHT)
        self._camera_alarm_meter = ctk.CTkProgressBar(inner, height=8)
        self._camera_alarm_meter.pack(fill=tk.X, pady=(4, 0))
        self._camera_alarm_meter.set(0)

        self._zoom_state_var = tk.StringVar(value="Zoom: normal")
        ctk.CTkLabel(
            inner, textvariable=self._zoom_state_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(6, 0))

    def set_zoom_state(self, label: str) -> None:
        if hasattr(self, "_zoom_state_var"):
            self._zoom_state_var.set(f"Zoom: {label}")

    def set_gate_state(self, open_: Optional[bool]) -> None:
        if open_ is None:
            self._gate_state_var.set("N/D")
            self.gate_state_badge.configure(text_color=th.TEXT_MUTED, fg_color=th.CHROME_BTN_BG)
        elif open_:
            self._gate_state_var.set("ABIERTA")
            self.gate_state_badge.configure(text_color=th.GATE_OPEN, fg_color=th.CHROME_BTN_BG)
        else:
            self._gate_state_var.set("CERRADA")
            self.gate_state_badge.configure(text_color=th.GATE_CLOSED, fg_color=th.CHROME_BTN_BG)

    def set_alarm_badge(self, state: Optional[bool]) -> None:
        if state is None:
            self.alarm_badge.configure(text_color=th.TEXT_MUTED, fg_color=th.CHROME_BTN_BG)
        elif state:
            self.alarm_badge.configure(text_color=th.STATE_ALARM, fg_color=th.CHROME_BTN_BG)
        else:
            self.alarm_badge.configure(text_color=th.STATE_OK, fg_color=th.CHROME_BTN_BG)
