"""Widgets reutilizables del visor."""

import tkinter as tk

import customtkinter as ctk

from foscam.ui import theme as th

AUDIO_DB_FLOOR = -60.0
AUDIO_DB_CEIL = 0.0


class StatusPill(ctk.CTkFrame):
    """Indicador de estado de conexión."""

    _STYLES = {
        "connecting": (th.WARNING, "Conectando"),
        "live": (th.SUCCESS, "En vivo"),
        "error": (th.DANGER, "Error"),
        "offline": (th.TEXT_MUTED, "Desconectado"),
        "reconnecting": (th.WARNING, "Reconectando"),
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=th.BG_CARD, corner_radius=12, **kwargs)
        self._dot = ctk.CTkLabel(self, text="●", width=16, font=ctk.CTkFont(size=14))
        self._dot.pack(side=tk.LEFT, padx=(10, 4), pady=6)
        self._label = ctk.CTkLabel(self, text="Conectando", font=th.small_font())
        self._label.pack(side=tk.LEFT, padx=(0, 10), pady=6)
        self.set_state("connecting")

    def set_state(self, state: str) -> None:
        color, text = self._STYLES.get(state, self._STYLES["connecting"])
        self._dot.configure(text_color=color)
        self._label.configure(text=text)


class SectionCard(ctk.CTkFrame):
    def __init__(self, master, title: str, **kwargs):
        super().__init__(master, fg_color=th.BG_CARD, corner_radius=10, **kwargs)
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
            self, width=width, height=height, bg=th.VU_TRACK,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill=tk.X)

    def set_gate_db(self, db: float) -> None:
        self._gate_db = db
        self.redraw(0.0)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.redraw(0.0)

    def _gate_x(self) -> int:
        db = max(AUDIO_DB_FLOOR, min(AUDIO_DB_CEIL, self._gate_db))
        span = AUDIO_DB_CEIL - AUDIO_DB_FLOOR
        if span <= 0:
            return 2
        ratio = (db - AUDIO_DB_FLOOR) / span
        return int(ratio * (self._meter_w - 4)) + 2

    def redraw(self, level_pct: float) -> None:
        c = self.canvas
        c.delete("all")
        if self._enabled and level_pct > 0:
            fill_w = max(1, int((level_pct / 100.0) * (self._meter_w - 2)))
            c.create_rectangle(1, 1, 1 + fill_w, self._meter_h - 1, fill=th.VU_FILL, outline="")
        c.create_line(self._gate_x(), 1, self._gate_x(), self._meter_h - 1, fill=th.DANGER, width=2)


class ReconnectBanner(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(
            master, fg_color=th.BG_CARD, corner_radius=12,
            border_width=1, border_color=th.WARNING, **kwargs,
        )
        ctk.CTkLabel(
            self, text="Reconectando stream…",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=th.WARNING,
        ).pack(padx=24, pady=16)
        self.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.place_forget()

    def show(self) -> None:
        self.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.lift()

    def hide(self) -> None:
        self.place_forget()


class PtzPad(ctk.CTkFrame):
    """Cruceta PTZ semitransparente."""

    def __init__(self, master, on_move, on_stop, **kwargs):
        super().__init__(
            master, fg_color=th.BG_CARD, corner_radius=10,
            border_width=1, border_color=th.ACCENT_MUTED, **kwargs,
        )
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
                fg_color=th.ACCENT_MUTED, hover_color=th.ACCENT,
                text_color=th.TEXT, font=ctk.CTkFont(size=13),
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
