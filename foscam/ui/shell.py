"""Layout principal del visor CustomTkinter."""

from typing import Optional

import tkinter as tk

import customtkinter as ctk

from foscam.ui import theme as th
from foscam.ui.widgets import PtzPad, ReconnectBanner, SectionCard, StatusPill, VuMeter

HELP_TEXT = """Atajos de teclado
• Flechas: mover PTZ (soltar para parar)
• 0: posición por defecto (ptzReset)
• a / z: subir / bajar volumen
• F11: pantalla completa
• Esc: salir de pantalla completa

Cruceta en el vídeo: mantener pulsado para mover la cámara.

Escala UI: --ui-scale 2.0 (o clave ui_scale en viewer.json)."""


class ViewerShell:
    """Construye y expone widgets del visor."""

    def __init__(self, root: ctk.CTk, host: str, camera_title: Optional[str] = None):
        self.root = root
        self.host = host
        self._camera_title = camera_title or host
        self._details_open = False
        self._help_open = False
        self._chrome_frames: list[ctk.CTkFrame] = []

    def build(
        self,
        *,
        initial_volume: int,
        gate_db: float,
        gate_min: int,
        gate_max: int,
        params_display: str,
        on_snapshot,
        on_disconnect,
        on_mute_toggle,
        on_volume_change,
        on_gate_change,
        on_gate_preset,
        on_display_resize,
        on_ptz_move,
        on_ptz_stop,
        on_toggle_details,
        on_toggle_help,
    ) -> None:
        self.root.configure(fg_color=th.BG_APP)

        # --- Footer (pack first = bottom) ---
        self.footer = ctk.CTkFrame(self.root, fg_color=th.BG_CARD, corner_radius=0, height=36)
        self.footer.pack(side=tk.BOTTOM, fill=tk.X)
        self._chrome_frames.append(self.footer)

        self.status_var = tk.StringVar(value=f"Conectando a {self.host}…")
        ctk.CTkLabel(
            self.footer, textvariable=self.status_var, anchor=tk.W,
            font=th.small_font(), text_color=th.TEXT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12, pady=8)

        self.btn_help_toggle = ctk.CTkButton(
            self.footer, text="Ayuda ▾", width=80, height=28,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.ACCENT_MUTED,
            command=on_toggle_help,
        )
        self.btn_help_toggle.pack(side=tk.RIGHT, padx=(4, 8), pady=4)

        self.btn_details_toggle = ctk.CTkButton(
            self.footer, text="Detalles ▾", width=90, height=28,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.ACCENT_MUTED,
            command=on_toggle_details,
        )
        self.btn_details_toggle.pack(side=tk.RIGHT, padx=4, pady=4)

        self.help_panel = ctk.CTkFrame(self.root, fg_color=th.BG_CARD)
        ctk.CTkLabel(
            self.help_panel, text=HELP_TEXT, justify=tk.LEFT,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, padx=16, pady=12)

        self.details_panel = ctk.CTkFrame(self.root, fg_color=th.BG_CARD)
        self._details_inner = ctk.CTkFrame(self.details_panel, fg_color="transparent")
        self._details_inner.pack(fill=tk.X, padx=16, pady=10)
        self._params_label = ctk.CTkLabel(
            self._details_inner, text=params_display, justify=tk.LEFT,
            font=th.small_font(), text_color=th.TEXT_MUTED, wraplength=900,
        )
        self._params_label.pack(anchor=tk.W)
        self.resolution_var = tk.StringVar(value="")
        self.display_size_var = tk.StringVar(value="")
        self.decode_backend_var = tk.StringVar(value="")
        self.volume_var = tk.StringVar(value="Vol: --")
        tech_row = ctk.CTkFrame(self._details_inner, fg_color="transparent")
        tech_row.pack(anchor=tk.W, pady=(6, 0))
        for var in (self.resolution_var, self.display_size_var, self.decode_backend_var, self.volume_var):
            ctk.CTkLabel(tech_row, textvariable=var, font=th.small_font(), text_color=th.TEXT_MUTED).pack(
                side=tk.LEFT, padx=(0, 16),
            )

        # --- Content ---
        self.content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._chrome_frames.append(self.content)

        # Sidebar
        self.sidebar = ctk.CTkFrame(
            self.content, fg_color=th.BG_CARD, width=th.sidebar_width(), corner_radius=0,
        )
        self.sidebar.pack(side=tk.RIGHT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
        self._chrome_frames.append(self.sidebar)

        self._build_sidebar(
            initial_volume, gate_db, gate_min, gate_max,
            on_volume_change, on_gate_change, on_gate_preset,
        )

        # Video area
        self.video_outer = ctk.CTkFrame(self.content, fg_color=th.BG_VIDEO, corner_radius=0)
        self.video_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.video_stack = ctk.CTkFrame(self.video_outer, fg_color=th.BG_VIDEO)
        self.video_stack.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.video_label = tk.Label(
            self.video_stack, text="Conectando…", bg=th.BG_VIDEO,
            fg=th.TEXT_MUTED, anchor=tk.CENTER,
        )
        self.video_label.pack(fill=tk.BOTH, expand=True)
        self.video_label.bind("<Configure>", on_display_resize)

        self.reconnect_banner = ReconnectBanner(self.video_stack)
        self.ptz_pad = PtzPad(self.video_stack, on_move=on_ptz_move, on_stop=on_ptz_stop)
        self.ptz_pad.place(relx=0.02, rely=0.98, anchor=tk.SW)
        self._ptz_hint = ctk.CTkLabel(
            self.video_stack, text="Flechas / cruceta = PTZ",
            font=ctk.CTkFont(size=10), text_color=th.TEXT_MUTED,
            fg_color=th.BG_CARD, corner_radius=6,
        )
        self._ptz_hint.place(relx=0.98, rely=0.98, anchor=tk.SE)

        # --- Toolbar ---
        self.toolbar = ctk.CTkFrame(self.root, fg_color=th.BG_CARD, corner_radius=0, height=56)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        self._chrome_frames.insert(0, self.toolbar)

        left = ctk.CTkFrame(self.toolbar, fg_color="transparent")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=12, pady=8)
        self.status_pill = StatusPill(left)
        self.status_pill.pack(side=tk.LEFT, padx=(0, 12))
        titles = ctk.CTkFrame(left, fg_color="transparent")
        titles.pack(side=tk.LEFT)
        self.title_var = tk.StringVar(value=self._camera_title)
        self.host_var = tk.StringVar(value=self.host)
        ctk.CTkLabel(titles, textvariable=self.title_var, font=th.title_font()).pack(anchor=tk.W)
        ctk.CTkLabel(
            titles, textvariable=self.host_var, font=th.muted_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W)

        right = ctk.CTkFrame(self.toolbar, fg_color="transparent")
        right.pack(side=tk.RIGHT, padx=12, pady=8)

        self.help_btn = ctk.CTkButton(
            right, text="?", width=36, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT, hover_color=th.ACCENT_MUTED,
            command=on_toggle_help,
        )
        self.help_btn.pack(side=tk.RIGHT, padx=4)

        self.mute_btn = ctk.CTkButton(
            right, text="Silenciar", width=90, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT, hover_color=th.ACCENT_MUTED,
            command=on_mute_toggle,
        )
        self.mute_btn.pack(side=tk.RIGHT, padx=4)

        self.disconnect_btn = ctk.CTkButton(
            right, text="Salir", width=80, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.DANGER,
            command=on_disconnect,
        )
        self.disconnect_btn.pack(side=tk.RIGHT, padx=4)

        self.snapshot_btn = ctk.CTkButton(
            right, text="Captura", width=90, height=32,
            fg_color=th.ACCENT, hover_color=th.ACCENT_HOVER, text_color=th.BG_APP,
            command=on_snapshot,
        )
        self.snapshot_btn.pack(side=tk.RIGHT, padx=4)

    def _build_sidebar(self, vol, gate_db, gate_min, gate_max, on_vol, on_gate, on_preset):
        scroll = ctk.CTkScrollableFrame(self.sidebar, fg_color=th.BG_CARD, width=th.sidebar_width() - 4)
        scroll.pack(fill=tk.BOTH, expand=True)

        audio = SectionCard(scroll, "AUDIO")
        audio.pack(fill=tk.X, padx=4, pady=(8, 4))

        ctk.CTkLabel(audio.body, text="Volumen", font=th.muted_font()).pack(anchor=tk.W)
        self.vol_slider = ctk.CTkSlider(
            audio.body, from_=0, to=100, number_of_steps=100,
            command=lambda v: on_vol(float(v)),
        )
        self.vol_slider.set(vol)
        self.vol_slider.pack(fill=tk.X, pady=(4, 0))
        self._vol_label_var = tk.StringVar(value=f"{int(vol)} %")
        ctk.CTkLabel(audio.body, textvariable=self._vol_label_var, font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            anchor=tk.W, pady=(0, 8),
        )

        ctk.CTkLabel(audio.body, text="Umbral de ruido", font=th.muted_font()).pack(anchor=tk.W)
        self.gate_slider = ctk.CTkSlider(
            audio.body, from_=gate_min, to=gate_max, number_of_steps=int(gate_max - gate_min),
            command=lambda v: on_gate(float(v)),
        )
        self.gate_slider.set(gate_db)
        self.gate_slider.pack(fill=tk.X, pady=(4, 0))
        self._gate_label_var = tk.StringVar(value=f"{gate_db:.0f} dB")
        ctk.CTkLabel(audio.body, textvariable=self._gate_label_var, font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            anchor=tk.W, pady=(0, 6),
        )

        presets = ctk.CTkFrame(audio.body, fg_color="transparent")
        presets.pack(fill=tk.X, pady=(0, 8))
        self._preset_buttons = {}
        for label, db in (("Llanto", -38), ("Suave", -48), ("Off", -90)):
            btn = ctk.CTkButton(
                presets, text=label, width=72, height=26,
                fg_color=th.ACCENT_MUTED, hover_color=th.ACCENT, text_color=th.TEXT,
                font=th.small_font(),
                command=lambda d=db: on_preset(d),
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._preset_buttons[float(db)] = btn
        self.highlight_gate_preset(gate_db)

        ctk.CTkLabel(audio.body, text="Nivel audio", font=th.muted_font()).pack(anchor=tk.W)
        mw, mh = th.audio_meter_size()
        self.vu_meter = VuMeter(audio.body, width=mw, height=mh)
        self.vu_meter.pack(fill=tk.X, pady=(4, 0))
        self._audio_level_label_var = tk.StringVar(value="— dB")
        ctk.CTkLabel(audio.body, textvariable=self._audio_level_label_var, font=th.small_font(), text_color=th.TEXT_MUTED).pack(
            anchor=tk.W,
        )
        ctk.CTkLabel(
            audio.body, text="Línea roja = umbral de ruido", font=ctk.CTkFont(size=9), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 4))

        sensors = SectionCard(scroll, "SENSORES")
        sensors.pack(fill=tk.X, padx=4, pady=4)

        ctk.CTkLabel(sensors.body, text="Movimiento (imagen)", font=th.muted_font()).pack(anchor=tk.W)
        self._motion_meter = ctk.CTkProgressBar(sensors.body, height=10)
        self._motion_meter.pack(fill=tk.X, pady=4)
        self._motion_meter.set(0)

        ctk.CTkLabel(sensors.body, text="Alarma cámara", font=th.muted_font()).pack(anchor=tk.W, pady=(8, 0))
        self._camera_alarm_meter = ctk.CTkProgressBar(sensors.body, height=10)
        self._camera_alarm_meter.pack(fill=tk.X, pady=4)
        self._camera_alarm_meter.set(0)
        self._camera_alarm_label_var = tk.StringVar(value="N/D")
        self.alarm_badge = ctk.CTkLabel(
            sensors.body, textvariable=self._camera_alarm_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
            fg_color=th.BG_APP, corner_radius=6,
        )
        self.alarm_badge.pack(anchor=tk.W, pady=(0, 4))

    def toggle_details(self) -> bool:
        self._details_open = not self._details_open
        if self._details_open:
            self.details_panel.pack(side=tk.BOTTOM, fill=tk.X, before=self.footer)
            self.btn_details_toggle.configure(text="Detalles ▴")
        else:
            self.details_panel.pack_forget()
            self.btn_details_toggle.configure(text="Detalles ▾")
        return self._details_open

    def toggle_help(self) -> bool:
        self._help_open = not self._help_open
        if self._help_open:
            self.help_panel.pack(side=tk.BOTTOM, fill=tk.X, before=self.footer)
            self.btn_help_toggle.configure(text="Ayuda ▴")
        else:
            self.help_panel.pack_forget()
            self.btn_help_toggle.configure(text="Ayuda ▾")
        return self._help_open

    def set_camera_title(self, name: str) -> None:
        self._camera_title = name
        self.title_var.set(name)

    def set_params_display(self, text: str) -> None:
        self._params_label.configure(text=text)

    def highlight_gate_preset(self, db: float) -> None:
        for preset_db, btn in getattr(self, "_preset_buttons", {}).items():
            if abs(preset_db - float(db)) < 0.5:
                btn.configure(fg_color=th.ACCENT, text_color=th.BG_APP)
            else:
                btn.configure(fg_color=th.ACCENT_MUTED, text_color=th.TEXT)

    def set_mute_button(self, muted: bool) -> None:
        if muted:
            self.mute_btn.configure(
                text="Activar audio", fg_color=th.DANGER,
                border_color=th.DANGER, border_width=1, text_color=th.BG_APP,
            )
        else:
            self.mute_btn.configure(
                text="Silenciar", fg_color="transparent",
                border_color=th.TEXT_MUTED, border_width=1, text_color=th.TEXT,
            )

    def set_ptz_hint_visible(self, visible: bool) -> None:
        if visible:
            self._ptz_hint.place(relx=0.98, rely=0.98, anchor=tk.SE)
        else:
            self._ptz_hint.place_forget()

    def set_alarm_badge(self, state: Optional[bool]) -> None:
        if state is None:
            self.alarm_badge.configure(text_color=th.TEXT_MUTED, fg_color=th.BG_APP)
        elif state:
            self.alarm_badge.configure(text_color=th.BG_APP, fg_color=th.WARNING)
        else:
            self.alarm_badge.configure(text_color=th.BG_APP, fg_color=th.SUCCESS)

    @property
    def chrome_for_fullscreen(self) -> list:
        return [self.toolbar, self.sidebar, self.footer, self.details_panel, self.help_panel]
