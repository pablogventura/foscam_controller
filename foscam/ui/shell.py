"""Layout principal del visor CustomTkinter (HUD overlay sobre vídeo)."""

from typing import List, Optional

import tkinter as tk

import customtkinter as ctk

from foscam.ui import theme as th
from foscam.ui.overlay import OverlayWindow
from foscam.ui.widgets import (
    IndicatorStrip,
    PtzPad,
    ReconnectBanner,
    SectionCard,
    StatusPill,
    VuMeter,
)

HELP_TEXT = """Atajos de teclado
• Flechas: mover PTZ (soltar para parar)
• 0: posición por defecto (ptzReset)
• a / s: subir / bajar volumen
• m: overlay movimiento en vivo
• v: zonas MD configuradas
• Z (mayúscula): zoom automático
• H: HUD completo ↔ modo indicadores
• [ / ]: colapsar / expandir panel lateral
• F11: pantalla completa (mantiene HUD)
• Esc: salir de pantalla completa

Cruceta en el vídeo: mantener pulsado para mover la cámara.

Movimiento: rojo = cambio en imagen; azul = zonas MD de la cámara.
Zoom auto: acerca al detectar movimiento y vuelve a vista normal al parar.

Umbral de ruido: solo pasa audio por encima del umbral (pre-volumen).
Indicador Puerta: ABIERTA / CERRADA según el bloque actual del stream.

NVIDIA: si hay GPU, se usa automáticamente (--no-nvidia fuerza CPU).
Diagnóstico gate: --audio-gate-debug o FOSCAM_AUDIO_GATE_DEBUG=1.

Escala UI: --ui-scale 2.0 (o clave ui_scale en viewer.json)."""


class ViewerShell:
    """Construye y expone widgets del visor con HUD semitransparente."""

    _OVERLAY_MARGIN = 8
    _TOOLBAR_H = 56
    _FOOTER_H = 36

    def __init__(self, root: ctk.CTk, host: str, camera_title: Optional[str] = None):
        self.root = root
        self.host = host
        self._camera_title = camera_title or host
        self._details_open = False
        self._help_open = False
        self._hud_mode = "full"
        self._sidebar_collapsed = False
        self._overlays: List[OverlayWindow] = []
        self._chrome_parts: List[ctk.CTkFrame] = []
        self._sync_after_id: Optional[str] = None
        self._alive = True

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
        on_toggle_hud=None,
        on_toggle_sidebar=None,
        motion=None,
        on_motion_change=None,
    ) -> None:
        self.root.configure(fg_color=th.BG_APP)
        scale = th.get_ui_scale()
        margin = int(self._OVERLAY_MARGIN * scale)
        toolbar_h = int(self._TOOLBAR_H * scale)
        footer_h = int(self._FOOTER_H * scale)

        self.video_stack = ctk.CTkFrame(self.root, fg_color=th.BG_VIDEO, corner_radius=0)
        self.video_stack.pack(fill=tk.BOTH, expand=True)

        self.video_label = tk.Label(
            self.video_stack, text="Conectando…", bg=th.BG_VIDEO,
            fg=th.TEXT_MUTED, anchor=tk.CENTER,
        )
        self.video_label.pack(fill=tk.BOTH, expand=True)
        self.video_label.bind("<Configure>", on_display_resize)

        self.reconnect_overlay = self._make_overlay(framed=True)
        self.reconnect_banner = ReconnectBanner(self.reconnect_overlay.body)
        self.reconnect_banner.pack(padx=8, pady=8)
        self.reconnect_overlay.hide()

        self.ptz_overlay = self._make_overlay(framed=False)
        self.ptz_pad = PtzPad(self.ptz_overlay.body, on_move=on_ptz_move, on_stop=on_ptz_stop)
        self.ptz_pad.pack(padx=4, pady=4)

        self.ptz_hint_overlay = self._make_overlay(framed=False)
        self._ptz_hint = ctk.CTkLabel(
            self.ptz_hint_overlay.body, text="Flechas / cruceta = PTZ",
            font=ctk.CTkFont(size=10), text_color=th.TEXT_MUTED,
        )
        self._ptz_hint.pack(padx=10, pady=6)

        self.indicator_overlay = self._make_overlay(framed=True)
        self.indicator_strip = IndicatorStrip(
            self.indicator_overlay.body, on_toggle_hud=on_toggle_hud or self.toggle_hud,
        )
        self.indicator_strip.pack(padx=4, pady=4)
        self.indicator_overlay.register_widget(self.indicator_strip)

        self._build_toolbar(
            on_snapshot, on_disconnect, on_mute_toggle,
            on_toggle_help, on_toggle_hud,
        )
        self._build_footer(on_toggle_details, on_toggle_help)
        self._build_sidebar(
            initial_volume, gate_db, gate_min, gate_max,
            on_volume_change, on_gate_change, on_gate_preset,
            on_toggle_sidebar,
            motion=motion,
            on_motion_change=on_motion_change,
        )
        self._build_floating_panels(params_display)

        self._margin = margin
        self._toolbar_h = toolbar_h
        self._footer_h = footer_h
        self.root.bind("<Configure>", self._schedule_sync_overlays, add="+")
        self.root.after(50, self.sync_overlays)
        self._apply_hud_layout()

    def _video_box(self) -> tuple[int, int]:
        try:
            w = max(1, self.video_stack.winfo_width())
            h = max(1, self.video_stack.winfo_height())
            return w, h
        except tk.TclError:
            return 1, 1

    def _register_chrome(self, frame: ctk.CTkFrame) -> ctk.CTkFrame:
        self._chrome_parts.append(frame)
        return frame

    def _show_chrome(self) -> None:
        self._sync_chrome_geometry()
        for part in self._chrome_parts:
            part.lift()

    def _hide_chrome(self) -> None:
        for part in self._chrome_parts:
            part.place_forget()

    def _sync_chrome_geometry(self) -> None:
        vw, vh = self._video_box()
        m = self._margin
        cw = self._content_width(vw)
        th_h = self._measure_toolbar_height()
        ft_h = self._measure_footer_height()
        self._toolbar_h = th_h
        self._footer_h = ft_h
        right_x = m + cw

        self.toolbar_left.place(x=m, y=m, anchor=tk.NW)
        self.toolbar_right.place(x=right_x, y=m, anchor=tk.NE)
        self.footer_left.place(x=m, y=vh - m, anchor=tk.SW)
        self.footer_right.place(x=right_x, y=vh - m, anchor=tk.SE)

    def _make_overlay(self, *, framed: bool = False, **kwargs) -> OverlayWindow:
        overlay = OverlayWindow(self.root, framed=framed, **kwargs)
        self._overlays.append(overlay)
        return overlay

    def _sidebar_pixel_width(self) -> int:
        if self._hud_mode == "minimal":
            self.indicator_strip.update_idletasks()
            return max(
                int(220 * th.get_ui_scale()),
                self.indicator_strip.winfo_reqwidth() + 16,
            )
        if self._sidebar_collapsed:
            return th.sidebar_collapsed_width()
        return th.sidebar_width()

    def _content_width(self, rw: int) -> int:
        return max(120, rw - self._sidebar_pixel_width() - 2 * self._margin)

    def _measure_toolbar_height(self) -> int:
        self.toolbar_left.update_idletasks()
        self.toolbar_right.update_idletasks()
        return max(
            int(44 * th.get_ui_scale()),
            self.toolbar_left.winfo_reqheight(),
            self.toolbar_right.winfo_reqheight(),
        ) + 4

    def _measure_footer_height(self) -> int:
        self.footer_left.update_idletasks()
        self.footer_right.update_idletasks()
        return max(
            int(28 * th.get_ui_scale()),
            self.footer_left.winfo_reqheight(),
            self.footer_right.winfo_reqheight(),
        ) + 4

    def _schedule_sync_overlays(self, _event=None) -> None:
        if not self._alive:
            return
        if self._sync_after_id is not None:
            return
        self._sync_after_id = self.root.after(80, self._do_scheduled_sync)

    def _do_scheduled_sync(self) -> None:
        self._sync_after_id = None
        self.sync_overlays()

    def _root_box(self) -> tuple[int, int, int, int]:
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = max(1, self.root.winfo_width())
        rh = max(1, self.root.winfo_height())
        return rx, ry, rw, rh

    def sync_overlays(self) -> None:
        if not self._alive:
            return
        try:
            rx, ry, rw, rh = self._root_box()
        except tk.TclError:
            return

        m = self._margin
        cw = self._content_width(rw)

        if self._hud_mode == "full":
            th_h = self._measure_toolbar_height()
            ft_h = self._measure_footer_height()
            self._toolbar_h = th_h
            self._footer_h = ft_h

            self._show_chrome()
            self._sync_sidebar_geometry(rx, ry, rw, rh, th_h, ft_h)
            self.indicator_overlay.hide()
            self._sync_ptz_geometry(rx, ry, rw, rh, ft_h, cw)
            self._sync_ptz_hint_geometry(rx, ry, rw, rh, ft_h, cw)
            self._sync_floating_panels_geometry(rx, ry, rw, rh, ft_h, cw)
        else:
            self._hide_chrome()
            self.sidebar.hide()
            self.details_overlay.hide()
            self.help_overlay.hide()
            self.ptz_overlay.hide()
            self.ptz_hint_overlay.hide()
            self._sync_indicator_geometry(rx, ry, rw, rh)

        self._update_hud_button()

        for overlay in self._overlays:
            try:
                if str(overlay.win.state()) != "withdrawn":
                    overlay.reset_idle()
            except tk.TclError:
                pass

    def _sync_sidebar_geometry(
        self, rx: int, ry: int, rw: int, rh: int, toolbar_h: int, footer_h: int,
    ) -> None:
        m = self._margin
        w = th.sidebar_collapsed_width() if self._sidebar_collapsed else th.sidebar_width()
        x = rx + rw - w - m
        y = ry + m
        side_h = max(120, rh - 2 * m)
        self.sidebar.set_geometry(x, y, w, side_h)
        self.sidebar.show()
        self._update_sidebar_inner()
        self.sidebar_toggle_btn.configure(text="«" if not self._sidebar_collapsed else "»")

    def _sync_indicator_geometry(self, rx: int, ry: int, rw: int, rh: int) -> None:
        m = self._margin
        self.indicator_strip.update_idletasks()
        w = max(int(220 * th.get_ui_scale()), self.indicator_strip.winfo_reqwidth() + 16)
        h = max(int(180 * th.get_ui_scale()), self.indicator_strip.winfo_reqheight() + 16)
        x = rx + rw - w - m
        y = ry + rh - h - m
        # Evitar solaparse con zona inferior izquierda reservada al vídeo limpio
        self.indicator_overlay.set_geometry(x, y, w, h)
        self.indicator_overlay.show()

    def _sync_ptz_geometry(
        self, rx: int, ry: int, rw: int, rh: int, footer_h: int, content_w: int,
    ) -> None:
        self.ptz_pad.update_idletasks()
        pw = self.ptz_pad.winfo_reqwidth() + 8
        ph = self.ptz_pad.winfo_reqheight() + 8
        x = rx + self._margin
        y = ry + rh - footer_h - self._margin - ph
        if y < ry + self._margin:
            y = ry + self._margin
        self.ptz_overlay.set_geometry(x, y, min(pw, content_w), ph)
        self.ptz_overlay.show()

    def _sync_ptz_hint_geometry(
        self, rx: int, ry: int, rw: int, rh: int, footer_h: int, content_w: int,
    ) -> None:
        self._ptz_hint.update_idletasks()
        w = min(content_w, self._ptz_hint.winfo_reqwidth() + 12)
        h = self._ptz_hint.winfo_reqheight() + 8
        x = rx + self._margin + max(0, content_w - w)
        y = ry + rh - footer_h - self._margin - h
        ptz_h = self.ptz_pad.winfo_reqheight() + 8
        ptz_y = ry + rh - footer_h - self._margin - ptz_h
        if abs(y - ptz_y) < h + 4:
            y = ptz_y - h - 4
        if y < ry + self._margin:
            self.ptz_hint_overlay.hide()
            return
        self.ptz_hint_overlay.set_geometry(x, y, w, h)
        self.ptz_hint_overlay.show()

    def _sync_floating_panels_geometry(
        self, rx: int, ry: int, rw: int, rh: int, footer_h: int, content_w: int,
    ) -> None:
        m = self._margin
        y_off = footer_h + m
        if self._details_open:
            self.details_panel.update_idletasks()
            dh = max(100, self.details_panel.winfo_reqheight() + 8)
            self.details_overlay.set_geometry(rx + m, ry + rh - y_off - dh, content_w, dh)
            self.details_overlay.show()
            y_off += dh
        else:
            self.details_overlay.hide()
        if self._help_open:
            self.help_panel.update_idletasks()
            hh = max(120, self.help_panel.winfo_reqheight() + 8)
            self.help_overlay.set_geometry(rx + m, ry + rh - y_off - hh, content_w, hh)
            self.help_overlay.show()
        else:
            self.help_overlay.hide()

    def _sync_reconnect_geometry(self) -> None:
        try:
            rx, ry, rw, rh = self._root_box()
        except tk.TclError:
            return
        self.reconnect_banner.update_idletasks()
        w = max(260, self.reconnect_banner.winfo_reqwidth() + 24)
        h = max(56, self.reconnect_banner.winfo_reqheight() + 24)
        x = rx + (rw - w) // 2
        y = ry + (rh - h) // 2
        self.reconnect_overlay.set_geometry(x, y, w, h)

    def _build_toolbar(self, on_snapshot, on_disconnect, on_mute, on_help, on_toggle_hud):
        host = self.video_label
        self.toolbar_left = self._register_chrome(
            ctk.CTkFrame(host, fg_color="transparent", corner_radius=0),
        )
        left = self.toolbar_left
        left_inner = ctk.CTkFrame(left, fg_color="transparent")
        left_inner.pack(side=tk.LEFT)
        self.status_pill = StatusPill(left_inner)
        self.status_pill.pack(side=tk.LEFT, padx=(0, 12))
        titles = ctk.CTkFrame(left_inner, fg_color="transparent")
        titles.pack(side=tk.LEFT)
        self.title_var = tk.StringVar(value=self._camera_title)
        self.host_var = tk.StringVar(value=self.host)
        ctk.CTkLabel(titles, textvariable=self.title_var, font=th.title_font()).pack(anchor=tk.W)
        ctk.CTkLabel(
            titles, textvariable=self.host_var, font=th.muted_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W)

        self.toolbar_right = self._register_chrome(
            ctk.CTkFrame(host, fg_color="transparent", corner_radius=0),
        )
        right = self.toolbar_right

        self.hud_btn = ctk.CTkButton(
            right, text="H", width=36, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT, hover_color=th.OVERLAY_HOVER,
            command=on_toggle_hud or self.toggle_hud,
        )
        self.hud_btn.pack(side=tk.RIGHT, padx=4)

        self.help_btn = ctk.CTkButton(
            right, text="?", width=36, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT, hover_color=th.OVERLAY_HOVER,
            command=on_help,
        )
        self.help_btn.pack(side=tk.RIGHT, padx=4)

        self.mute_btn = ctk.CTkButton(
            right, text="Silenciar", width=90, height=32,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT, hover_color=th.OVERLAY_HOVER,
            command=on_mute,
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
            fg_color="transparent", border_width=1, border_color=th.ACCENT,
            hover_color=th.OVERLAY_HOVER, text_color=th.ACCENT,
            command=on_snapshot,
        )
        self.snapshot_btn.pack(side=tk.RIGHT, padx=4)

    def _build_footer(self, on_toggle_details, on_toggle_help):
        host = self.video_label
        self.footer_left = self._register_chrome(
            ctk.CTkFrame(host, fg_color="transparent", corner_radius=0),
        )
        self.status_var = tk.StringVar(value=f"Conectando a {self.host}…")
        ctk.CTkLabel(
            self.footer_left, textvariable=self.status_var, anchor=tk.W,
            font=th.small_font(), text_color=th.TEXT,
        ).pack(side=tk.LEFT, padx=(4, 8), pady=2)

        self.footer_right = self._register_chrome(
            ctk.CTkFrame(host, fg_color="transparent", corner_radius=0),
        )
        panel = self.footer_right

        self.btn_help_toggle = ctk.CTkButton(
            panel, text="Ayuda ▾", width=80, height=28,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.OVERLAY_HOVER,
            command=on_toggle_help,
        )
        self.btn_help_toggle.pack(side=tk.RIGHT, padx=(2, 4), pady=2)

        self.btn_details_toggle = ctk.CTkButton(
            panel, text="Detalles ▾", width=90, height=26,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.OVERLAY_HOVER,
            command=on_toggle_details,
        )
        self.btn_details_toggle.pack(side=tk.RIGHT, padx=4, pady=4)

    def _build_sidebar(
        self, vol, gate_db, gate_min, gate_max, on_vol, on_gate, on_preset, on_toggle_sidebar,
        motion=None, on_motion_change=None,
    ):
        self.sidebar = self._make_overlay(framed=True)
        panel = self.sidebar.body
        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill=tk.X, padx=8, pady=(8, 4))
        ctk.CTkLabel(header, text="Controles", font=th.section_font(), text_color=th.ACCENT).pack(
            side=tk.LEFT,
        )
        self.sidebar_toggle_btn = ctk.CTkButton(
            header, text="«", width=28, height=24,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.OVERLAY_HOVER,
            command=on_toggle_sidebar or self.toggle_sidebar_collapsed,
        )
        self.sidebar_toggle_btn.pack(side=tk.RIGHT)

        self._sidebar_body = ctk.CTkFrame(panel, fg_color="transparent")
        self._sidebar_body.pack(fill=tk.BOTH, expand=True)

        self._sidebar_collapsed_view = ctk.CTkFrame(panel, fg_color="transparent")
        ctk.CTkButton(
            self._sidebar_collapsed_view, text="»", width=28, height=28,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            text_color=th.TEXT_MUTED, hover_color=th.OVERLAY_HOVER,
            command=on_toggle_sidebar or self.toggle_sidebar_collapsed,
        ).pack(pady=(8, 4))
        cw = max(32, th.sidebar_collapsed_width() - 20)
        _, mh = th.indicator_meter_size()
        self._sidebar_mini_motion = ctk.CTkProgressBar(
            self._sidebar_collapsed_view, width=cw, height=6, orientation="horizontal",
        )
        self._sidebar_mini_motion.pack(padx=6, pady=(2, 4))
        self._sidebar_mini_motion.set(0)
        self.sidebar_mini_vu = VuMeter(self._sidebar_collapsed_view, width=cw, height=mh)
        self.sidebar_mini_vu.pack(padx=6, pady=4)
        self._sidebar_mini_db_var = tk.StringVar(value="— dB")
        ctk.CTkLabel(
            self._sidebar_collapsed_view, textvariable=self._sidebar_mini_db_var,
            font=ctk.CTkFont(size=9), text_color=th.TEXT_MUTED, wraplength=cw + 8,
        ).pack(padx=4, pady=(0, 8))

        scroll = ctk.CTkScrollableFrame(
            self._sidebar_body, fg_color="transparent", width=th.sidebar_width() - 24,
        )
        scroll.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 8))

        activity = SectionCard(scroll, "ACTIVIDAD")
        activity.pack(fill=tk.X, padx=4, pady=(4, 4))

        ctk.CTkLabel(activity.body, text="Movimiento (imagen)", font=th.muted_font()).pack(anchor=tk.W)
        self._motion_meter = ctk.CTkProgressBar(activity.body, height=10)
        self._motion_meter.pack(fill=tk.X, pady=(4, 8))
        self._motion_meter.set(0)

        ctk.CTkLabel(activity.body, text="Nivel audio", font=th.muted_font()).pack(anchor=tk.W)
        mw, mh = th.audio_meter_size()
        self.vu_meter = VuMeter(activity.body, width=mw, height=mh)
        self.vu_meter.pack(fill=tk.X, pady=(4, 0))
        self._audio_level_label_var = tk.StringVar(value="— dB")
        ctk.CTkLabel(
            activity.body, textvariable=self._audio_level_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W)
        ctk.CTkLabel(
            activity.body, text="Línea roja = umbral de ruido",
            font=ctk.CTkFont(size=9), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 4))

        audio = SectionCard(scroll, "AUDIO")
        audio.pack(fill=tk.X, padx=4, pady=4)

        ctk.CTkLabel(audio.body, text="Volumen", font=th.muted_font()).pack(anchor=tk.W)
        self.vol_slider = ctk.CTkSlider(
            audio.body, from_=0, to=100, number_of_steps=100,
            command=lambda v: on_vol(float(v)),
        )
        self.vol_slider.set(vol)
        self.vol_slider.pack(fill=tk.X, pady=(4, 0))
        self._vol_label_var = tk.StringVar(value=f"{int(vol)} %")
        ctk.CTkLabel(
            audio.body, textvariable=self._vol_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 8))

        ctk.CTkLabel(audio.body, text="Umbral de ruido", font=th.muted_font()).pack(anchor=tk.W)
        self.gate_slider = ctk.CTkSlider(
            audio.body, from_=gate_min, to=gate_max, number_of_steps=int(gate_max - gate_min),
            command=lambda v: on_gate(float(v)),
        )
        self.gate_slider.set(gate_db)
        self.gate_slider.pack(fill=tk.X, pady=(4, 0))
        self._gate_label_var = tk.StringVar(value=f"{gate_db:.0f} dB")
        ctk.CTkLabel(
            audio.body, textvariable=self._gate_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 6))

        presets = ctk.CTkFrame(audio.body, fg_color="transparent")
        presets.pack(fill=tk.X, pady=(0, 8))
        self._preset_buttons = {}
        for label, db in (("Llanto", -38), ("Suave", -48), ("Off", -90)):
            btn = ctk.CTkButton(
                presets, text=label, width=72, height=26,
                fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
                hover_color=th.OVERLAY_HOVER, text_color=th.TEXT,
                font=th.small_font(),
                command=lambda d=db: on_preset(d),
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._preset_buttons[float(db)] = btn
        self.highlight_gate_preset(gate_db)

        gate_row = ctk.CTkFrame(audio.body, fg_color="transparent")
        gate_row.pack(fill=tk.X, pady=(0, 4))
        ctk.CTkLabel(gate_row, text="Puerta", font=th.muted_font()).pack(side=tk.LEFT)
        self._gate_state_var = tk.StringVar(value="N/D")
        self.gate_state_badge = ctk.CTkLabel(
            gate_row, textvariable=self._gate_state_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
            fg_color="transparent", corner_radius=6,
        )
        self.gate_state_badge.pack(side=tk.RIGHT)

        alarm = SectionCard(scroll, "ALARMA")
        alarm.pack(fill=tk.X, padx=4, pady=4)

        ctk.CTkLabel(alarm.body, text="Alarma cámara", font=th.muted_font()).pack(anchor=tk.W)
        self._camera_alarm_meter = ctk.CTkProgressBar(alarm.body, height=10)
        self._camera_alarm_meter.pack(fill=tk.X, pady=4)
        self._camera_alarm_meter.set(0)
        self._camera_alarm_label_var = tk.StringVar(value="N/D")
        self.alarm_badge = ctk.CTkLabel(
            alarm.body, textvariable=self._camera_alarm_label_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
            fg_color="transparent", corner_radius=6,
        )
        self.alarm_badge.pack(anchor=tk.W, pady=(0, 4))

        if motion is not None and on_motion_change is not None:
            self._build_motion_section(scroll, motion, on_motion_change)

        self.resolution_var = tk.StringVar(value="")
        self.display_size_var = tk.StringVar(value="")
        self.decode_backend_var = tk.StringVar(value="")
        self.volume_var = tk.StringVar(value="Vol: --")
        self.sidebar.register_widget(panel)

    def _build_motion_section(self, scroll, motion, on_change):
        mov = SectionCard(scroll, "MOVIMIENTO")
        mov.pack(fill=tk.X, padx=4, pady=4)
        body = mov.body

        self._motion_live_var = tk.BooleanVar(value=bool(motion.get("show_live_overlay", False)))
        ctk.CTkSwitch(
            body, text="Overlay movimiento", variable=self._motion_live_var,
            command=lambda: on_change("show_live_overlay", self._motion_live_var.get()),
        ).pack(anchor=tk.W, pady=2)

        self._motion_zones_var = tk.BooleanVar(value=bool(motion.get("show_configured_zones", False)))
        ctk.CTkSwitch(
            body, text="Zonas MD cámara", variable=self._motion_zones_var,
            command=lambda: on_change("show_configured_zones", self._motion_zones_var.get()),
        ).pack(anchor=tk.W, pady=2)

        self._motion_auto_var = tk.BooleanVar(value=bool(motion.get("auto_zoom_enabled", False)))
        ctk.CTkSwitch(
            body, text="Zoom automático", variable=self._motion_auto_var,
            command=lambda: on_change("auto_zoom.enabled", self._motion_auto_var.get()),
        ).pack(anchor=tk.W, pady=2)

        self._motion_zoom_state_var = tk.StringVar(value="Zoom: normal")
        ctk.CTkLabel(
            body, textvariable=self._motion_zoom_state_var,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 4))

        self._motion_ptz_caps_var = tk.StringVar(value="PTZ: …")
        ctk.CTkLabel(
            body, textvariable=self._motion_ptz_caps_var,
            font=ctk.CTkFont(size=9), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 4))

        ctk.CTkLabel(body, text="Sensibilidad", font=th.muted_font()).pack(anchor=tk.W)
        self._motion_sens_slider = ctk.CTkSlider(
            body, from_=0, to=100, number_of_steps=100,
            command=lambda v: on_change("sensitivity", float(v)),
        )
        self._motion_sens_slider.set(float(motion.get("sensitivity", 50)))
        self._motion_sens_slider.pack(fill=tk.X, pady=(2, 4))

        ctk.CTkLabel(body, text="Segundos sin movimiento", font=th.muted_font()).pack(anchor=tk.W)
        self._motion_return_slider = ctk.CTkSlider(
            body, from_=1, to=30, number_of_steps=29,
            command=lambda v: on_change("auto_zoom.return_sec", float(v)),
        )
        self._motion_return_slider.set(float(motion.get("auto_zoom_return_sec", 5)))
        self._motion_return_slider.pack(fill=tk.X, pady=(2, 4))

        ctk.CTkLabel(body, text="Modo zoom", font=th.muted_font()).pack(anchor=tk.W)
        self._motion_mode = ctk.CTkOptionMenu(
            body,
            values=["auto", "digital", "ptz", "ptz_pan_digital_zoom"],
            command=lambda v: on_change("auto_zoom.mode", v),
        )
        self._motion_mode.set(str(motion.get("auto_zoom_mode", "auto")))
        self._motion_mode.pack(fill=tk.X, pady=(2, 4))

        ptz_row = ctk.CTkFrame(body, fg_color="transparent")
        ptz_row.pack(fill=tk.X, pady=(0, 4))
        ctk.CTkLabel(ptz_row, text="Vel. PTZ", font=th.muted_font()).pack(side=tk.LEFT)
        self._motion_ptz_speed = ctk.CTkSlider(
            ptz_row, from_=0, to=4, number_of_steps=4, width=100,
            command=lambda v: on_change("auto_zoom.ptz_speed", int(round(float(v)))),
        )
        self._motion_ptz_speed.set(float(motion.get("auto_zoom_ptz_speed", 2)))
        self._motion_ptz_speed.pack(side=tk.RIGHT)

        presets = ctk.CTkFrame(body, fg_color="transparent")
        presets.pack(fill=tk.X, pady=(0, 4))
        for label, key in (("Sensible", "sensitive"), ("Normal", "normal"), ("Estricto", "strict")):
            ctk.CTkButton(
                presets, text=label, width=72, height=26,
                fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
                hover_color=th.OVERLAY_HOVER, text_color=th.TEXT, font=th.small_font(),
                command=lambda k=key: on_change("preset", k),
            ).pack(side=tk.LEFT, padx=(0, 6))

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill=tk.X, pady=(0, 4))
        ctk.CTkButton(
            btn_row, text="Actualizar zonas", width=110, height=26,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            hover_color=th.OVERLAY_HOVER, font=th.small_font(),
            command=lambda: on_change("refresh_zones", True),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Probar PTZ", width=90, height=26,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            hover_color=th.OVERLAY_HOVER, font=th.small_font(),
            command=lambda: on_change("test_ptz", True),
        ).pack(side=tk.LEFT)

        ctk.CTkLabel(
            body, text="Rojo = movimiento · Azul = zonas MD",
            font=ctk.CTkFont(size=9), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, pady=(0, 4))

        self._motion_advanced_open = False
        self._motion_adv_btn = ctk.CTkButton(
            body, text="Avanzado ▾", width=100, height=24,
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            hover_color=th.OVERLAY_HOVER, font=th.small_font(),
            command=self._toggle_motion_advanced,
        )
        self._motion_adv_btn.pack(anchor=tk.W, pady=(0, 4))

        self._motion_advanced = ctk.CTkFrame(body, fg_color="transparent")
        adv = self._motion_advanced
        self._motion_adv_vars = {}
        for key, label, default in (
            ("debug_fsm_overlay", "Debug FSM", False),
            ("ignore_camera_md_disabled", "Ignorar MD off en cámara", False),
            ("require_audio_gate", "Zoom solo con audio", False),
            ("ignore_zones_outside_md", "Solo movimiento en zonas MD", False),
            ("flash_alarm_border", "Borde alarma CGI", False),
            ("privacy_mask_overlay", "Máscaras privacidad OSD", False),
            ("heatmap.enabled", "Mapa de calor", False),
            ("snapshot_on_motion.enabled", "Snapshot al movimiento", False),
            ("auto_zoom.return_use_ptz_reset", "ptzReset al volver", True),
            ("auto_zoom.return_use_zoom_out", "zoomOut al volver", True),
        ):
            var = tk.BooleanVar(value=bool(motion.get(key.replace(".", "_"), default)))
            if "." in key:
                parts = key.split(".")
                nested = motion
                for p in parts[:-1]:
                    nested = nested.get(p, {}) if isinstance(nested, dict) else {}
                var.set(bool(nested.get(parts[-1], default)) if isinstance(nested, dict) else default)
            self._motion_adv_vars[key] = var
            ctk.CTkSwitch(
                adv, text=label, variable=var,
                command=lambda k=key, v=var: on_change(k, v.get()),
            ).pack(anchor=tk.W, pady=1)

        ctk.CTkLabel(adv, text="Perfil", font=th.muted_font()).pack(anchor=tk.W, pady=(6, 0))
        prof_row = ctk.CTkFrame(adv, fg_color="transparent")
        prof_row.pack(fill=tk.X, pady=2)
        self._motion_profile_menu = ctk.CTkOptionMenu(
            prof_row, values=["(ninguno)"] + list(motion.get("profile_names", [])),
            command=lambda v: on_change("active_profile", None if v == "(ninguno)" else v),
        )
        self._motion_profile_menu.set(motion.get("active_profile") or "(ninguno)")
        self._motion_profile_menu.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ctk.CTkButton(
            prof_row, text="Guardar", width=56, height=24, font=th.small_font(),
            command=lambda: on_change("save_profile_prompt", True),
        ).pack(side=tk.RIGHT, padx=(4, 0))
        ctk.CTkButton(
            prof_row, text="Borrar", width=56, height=24, font=th.small_font(),
            fg_color="transparent", border_width=1, border_color=th.TEXT_MUTED,
            command=lambda: on_change("delete_profile", self._motion_profile_menu.get()),
        ).pack(side=tk.RIGHT, padx=(4, 0))

        ctk.CTkLabel(adv, text="Detección", font=th.muted_font()).pack(anchor=tk.W, pady=(8, 0))
        self._add_motion_slider(
            adv, "Retardo activación (s)", "trigger_hold_sec", 0.0, 3.0, 30,
            float(motion.get("trigger_hold_sec", 0.5)), on_change,
        )
        self._add_motion_slider(
            adv, "Umbral movimiento", "trigger_level", 0, 100, 100,
            float(motion.get("trigger_level", 15)), on_change,
        )
        self._add_motion_slider(
            adv, "Zoom máx. digital", "max_digital", 1.0, 5.0, 40,
            float(motion.get("auto_zoom_max_digital", 3.0)), on_change, key_prefix="auto_zoom.",
        )

        ctk.CTkLabel(adv, text="Overlay live", font=th.muted_font()).pack(anchor=tk.W, pady=(8, 0))
        self._motion_live_style = ctk.CTkOptionMenu(
            adv, values=["boxes", "filled", "contour", "crosshair"],
            command=lambda v: on_change("live_overlay_style", v),
        )
        self._motion_live_style.set(str(motion.get("live_overlay_style", "boxes")))
        self._motion_live_style.pack(fill=tk.X, pady=2)

        ctk.CTkLabel(adv, text="Zonas MD fuente", font=th.muted_font()).pack(anchor=tk.W, pady=(8, 0))
        self._motion_zones_source = ctk.CTkOptionMenu(
            adv, values=["auto", "config", "config1", "config2"],
            command=lambda v: on_change("zones_config_source", v),
        )
        self._motion_zones_source.set(str(motion.get("zones_config_source", "auto")))
        self._motion_zones_source.pack(fill=tk.X, pady=2)

        ctk.CTkLabel(adv, text="Carpeta snapshots", font=th.muted_font()).pack(anchor=tk.W, pady=(8, 0))
        self._motion_snap_dir = ctk.CTkEntry(adv, placeholder_text="~/Pictures/foscam-motion")
        self._motion_snap_dir.insert(0, str(motion.get("snapshot_dir", "~/Pictures/foscam-motion")))
        self._motion_snap_dir.bind(
            "<FocusOut>",
            lambda e: on_change("snapshot_on_motion.dir", self._motion_snap_dir.get()),
        )
        self._motion_snap_dir.pack(fill=tk.X, pady=2)

        self._add_motion_slider(
            adv, "Heatmap decay (s)", "decay_sec", 30, 600, 57,
            float(motion.get("heatmap_decay_sec", 120)), on_change, key_prefix="heatmap.",
        )

        self._motion_show_legend = tk.BooleanVar(value=bool(motion.get("show_legend", True)))
        ctk.CTkSwitch(
            adv, text="Mostrar leyenda", variable=self._motion_show_legend,
            command=lambda: on_change("show_legend", self._motion_show_legend.get()),
        ).pack(anchor=tk.W, pady=2)

        self._on_motion_change = on_change

    def _add_motion_slider(self, parent, label, key, vmin, vmax, steps, initial, on_change, key_prefix=""):
        ctk.CTkLabel(parent, text=label, font=th.muted_font()).pack(anchor=tk.W)
        full_key = f"{key_prefix}{key}" if key_prefix else key
        slider = ctk.CTkSlider(
            parent, from_=vmin, to=vmax, number_of_steps=max(1, steps),
            command=lambda v, k=full_key: on_change(k, float(v) if isinstance(vmin, float) else int(round(float(v)))),
        )
        slider.set(initial)
        slider.pack(fill=tk.X, pady=(2, 4))
        attr = f"_motion_slider_{full_key.replace('.', '_')}"
        setattr(self, attr, slider)

    def _toggle_motion_advanced(self) -> None:
        self._motion_advanced_open = not self._motion_advanced_open
        if self._motion_advanced_open:
            self._motion_advanced.pack(fill=tk.X, pady=(0, 4))
            self._motion_adv_btn.configure(text="Avanzado ▴")
        else:
            self._motion_advanced.pack_forget()
            self._motion_adv_btn.configure(text="Avanzado ▾")

    def set_motion_zoom_state(self, label: str) -> None:
        if hasattr(self, "_motion_zoom_state_var"):
            self._motion_zoom_state_var.set(f"Zoom: {label}")

    def set_motion_ptz_caps(self, has_ptz: bool, has_optical: bool) -> None:
        if not hasattr(self, "_motion_ptz_caps_var"):
            return
        parts = []
        parts.append("PTZ" if has_ptz else "sin PTZ")
        parts.append("zoom óptico" if has_optical else "zoom digital")
        self._motion_ptz_caps_var.set(" · ".join(parts))

    def set_motion_profile_names(self, names: list, active: Optional[str] = None) -> None:
        if not hasattr(self, "_motion_profile_menu"):
            return
        values = ["(ninguno)"] + list(names)
        self._motion_profile_menu.configure(values=values)
        self._motion_profile_menu.set(active or "(ninguno)")

    def sync_motion_toggles(self, show_live: bool, show_zones: bool, auto_zoom: bool) -> None:
        if hasattr(self, "_motion_live_var"):
            self._motion_live_var.set(show_live)
        if hasattr(self, "_motion_zones_var"):
            self._motion_zones_var.set(show_zones)
        if hasattr(self, "_motion_auto_var"):
            self._motion_auto_var.set(auto_zoom)

    def sync_motion_from_settings(self, motion: dict) -> None:
        """Refresca controles del sidebar tras preset/perfil."""
        self.sync_motion_toggles(
            motion.get("show_live_overlay", False),
            motion.get("show_configured_zones", False),
            motion.get("auto_zoom_enabled", False),
        )
        if hasattr(self, "_motion_sens_slider"):
            self._motion_sens_slider.set(float(motion.get("sensitivity", 50)))
        if hasattr(self, "_motion_return_slider"):
            self._motion_return_slider.set(float(motion.get("auto_zoom_return_sec", 5)))
        if hasattr(self, "_motion_mode"):
            self._motion_mode.set(str(motion.get("auto_zoom_mode", "auto")))
        if hasattr(self, "_motion_ptz_speed"):
            self._motion_ptz_speed.set(float(motion.get("auto_zoom_ptz_speed", 2)))
        if hasattr(self, "_motion_live_style"):
            self._motion_live_style.set(str(motion.get("live_overlay_style", "boxes")))
        if hasattr(self, "_motion_zones_source"):
            self._motion_zones_source.set(str(motion.get("zones_config_source", "auto")))
        if hasattr(self, "_motion_snap_dir"):
            self._motion_snap_dir.delete(0, tk.END)
            self._motion_snap_dir.insert(0, str(motion.get("snapshot_dir", "~/Pictures/foscam-motion")))
        for key, var in getattr(self, "_motion_adv_vars", {}).items():
            parts = key.split(".")
            nested = motion
            for p in parts[:-1]:
                nested = nested.get(p, {}) if isinstance(nested, dict) else {}
            val = nested.get(parts[-1]) if isinstance(nested, dict) else None
            if val is None:
                flat = motion.get(key.replace(".", "_"))
                if flat is not None:
                    val = flat
            if val is not None:
                var.set(bool(val))
        if motion.get("active_profile"):
            self.set_motion_profile_names(motion.get("profile_names", []), motion.get("active_profile"))

    def _build_floating_panels(self, params_display: str):
        self.details_overlay = self._make_overlay(framed=False)
        self.details_panel = ctk.CTkFrame(self.details_overlay.body, fg_color="transparent")
        self.details_panel.pack(fill=tk.BOTH, expand=True)
        self._details_inner = ctk.CTkFrame(self.details_panel, fg_color="transparent")
        self._details_inner.pack(fill=tk.X, padx=16, pady=10)
        self._params_label = ctk.CTkLabel(
            self._details_inner, text=params_display, justify=tk.LEFT,
            font=th.small_font(), text_color=th.TEXT_MUTED, wraplength=900,
        )
        self._params_label.pack(anchor=tk.W)
        tech_row = ctk.CTkFrame(self._details_inner, fg_color="transparent")
        tech_row.pack(anchor=tk.W, pady=(6, 0))
        for var in (self.resolution_var, self.display_size_var, self.decode_backend_var, self.volume_var):
            ctk.CTkLabel(
                tech_row, textvariable=var, font=th.small_font(), text_color=th.TEXT_MUTED,
            ).pack(side=tk.LEFT, padx=(0, 16))
        self.details_overlay.hide()

        self.help_overlay = self._make_overlay(framed=False)
        self.help_panel = ctk.CTkFrame(self.help_overlay.body, fg_color="transparent")
        self.help_panel.pack(fill=tk.BOTH, expand=True)
        ctk.CTkLabel(
            self.help_panel, text=HELP_TEXT, justify=tk.LEFT,
            font=th.small_font(), text_color=th.TEXT_MUTED,
        ).pack(anchor=tk.W, padx=16, pady=12)
        self.help_overlay.hide()

    @property
    def hud_mode(self) -> str:
        return self._hud_mode

    def set_hud_mode(self, mode: str) -> None:
        if mode not in ("full", "minimal"):
            return
        self._hud_mode = mode
        if mode == "minimal":
            self._details_open = False
            self._help_open = False
            self.btn_details_toggle.configure(text="Detalles ▾")
            self.btn_help_toggle.configure(text="Ayuda ▾")
        self._apply_hud_layout()

    set_hud_visible = set_hud_mode

    def toggle_hud(self) -> str:
        self.set_hud_mode("minimal" if self._hud_mode == "full" else "full")
        return self._hud_mode

    def set_sidebar_collapsed(self, collapsed: bool) -> None:
        self._sidebar_collapsed = bool(collapsed)
        if self._hud_mode == "full":
            self.sync_overlays()

    def toggle_sidebar_collapsed(self) -> bool:
        if self._hud_mode != "full":
            return self._sidebar_collapsed
        self._sidebar_collapsed = not self._sidebar_collapsed
        self.sync_overlays()
        return self._sidebar_collapsed

    def _apply_hud_layout(self) -> None:
        self.sync_overlays()

    def _update_sidebar_inner(self) -> None:
        if self._sidebar_collapsed:
            self._sidebar_body.pack_forget()
            self._sidebar_collapsed_view.pack(fill=tk.BOTH, expand=True)
        else:
            self._sidebar_collapsed_view.pack_forget()
            self._sidebar_body.pack(fill=tk.BOTH, expand=True)

    def _update_hud_button(self) -> None:
        if self._hud_mode == "minimal":
            self.hud_btn.configure(text="H●", fg_color="transparent", border_color=th.ACCENT)
            self.indicator_strip.hud_btn.configure(
                text="H●", fg_color="transparent", border_color=th.ACCENT,
            )
        else:
            self.hud_btn.configure(text="H", fg_color="transparent", border_color=th.TEXT_MUTED)
            self.indicator_strip.hud_btn.configure(
                text="H", fg_color="transparent", border_color=th.TEXT_MUTED,
            )

    def set_connection_state(self, state: str) -> None:
        self.status_pill.set_state(state)
        self.indicator_strip.status_pill.set_state(state)

    def toggle_details(self) -> bool:
        if self._hud_mode != "full":
            return False
        self._details_open = not self._details_open
        if self._details_open:
            self.btn_details_toggle.configure(text="Detalles ▴")
        else:
            self.btn_details_toggle.configure(text="Detalles ▾")
        self.sync_overlays()
        return self._details_open

    def toggle_help(self) -> bool:
        if self._hud_mode != "full":
            return False
        self._help_open = not self._help_open
        if self._help_open:
            self.btn_help_toggle.configure(text="Ayuda ▴")
        else:
            self.btn_help_toggle.configure(text="Ayuda ▾")
        self.sync_overlays()
        return self._help_open

    def set_camera_title(self, name: str) -> None:
        self._camera_title = name
        self.title_var.set(name)

    def set_params_display(self, text: str) -> None:
        self._params_label.configure(text=text)

    def highlight_gate_preset(self, db: float) -> None:
        for preset_db, btn in getattr(self, "_preset_buttons", {}).items():
            if abs(preset_db - float(db)) < 0.5:
                btn.configure(fg_color=th.ACCENT_MUTED, border_color=th.ACCENT, text_color=th.TEXT)
            else:
                btn.configure(
                    fg_color="transparent", border_color=th.TEXT_MUTED, text_color=th.TEXT,
                )

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
        if visible and self._hud_mode == "full":
            self.ptz_hint_overlay.show()
        else:
            self.ptz_hint_overlay.hide()
        self.sync_overlays()

    def _apply_gate_badge(self, badge, var, open_: Optional[bool]) -> None:
        if open_ is None:
            var.set("N/D")
            badge.configure(text_color=th.TEXT_MUTED, fg_color="transparent")
        elif open_:
            var.set("ABIERTA")
            badge.configure(text_color=th.GATE_OPEN, fg_color="transparent")
        else:
            var.set("CERRADA")
            badge.configure(text_color=th.GATE_CLOSED, fg_color="transparent")

    def set_gate_state(self, open_: Optional[bool]) -> None:
        self._apply_gate_badge(self.gate_state_badge, self._gate_state_var, open_)
        self.indicator_strip.set_gate_state(open_)

    def _apply_alarm_badge(self, badge, state: Optional[bool]) -> None:
        if state is None:
            badge.configure(text_color=th.TEXT_MUTED, fg_color="transparent")
        elif state:
            badge.configure(text_color=th.STATE_ALARM, fg_color="transparent")
        else:
            badge.configure(text_color=th.STATE_OK, fg_color="transparent")

    def set_alarm_badge(self, state: Optional[bool]) -> None:
        self._apply_alarm_badge(self.alarm_badge, state)
        self.indicator_strip.set_alarm_badge(state)

    def show_reconnect_banner(self) -> None:
        self._sync_reconnect_geometry()
        self.reconnect_overlay.show()

    def hide_reconnect_banner(self) -> None:
        self.reconnect_overlay.hide()

    def destroy_overlays(self) -> None:
        self._alive = False
        if self._sync_after_id is not None:
            try:
                self.root.after_cancel(self._sync_after_id)
            except tk.TclError:
                pass
            self._sync_after_id = None
        for overlay in self._overlays:
            overlay.destroy()
        self._overlays.clear()

    @property
    def sidebar_collapsed(self) -> bool:
        return self._sidebar_collapsed

    @property
    def chrome_for_fullscreen(self) -> list:
        return [
            *self._chrome_parts,
            self.sidebar,
            self.details_overlay, self.help_overlay,
            self.indicator_overlay, self.ptz_overlay, self.ptz_hint_overlay,
            self.reconnect_overlay,
        ]
