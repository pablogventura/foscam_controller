"""Paneles HUD flotantes con transparencia real (Toplevel + alpha)."""

from __future__ import annotations

import tkinter as tk
from typing import Optional

import customtkinter as ctk

from foscam.ui import theme as th

OVERLAY_IDLE_ALPHA = 0.32
OVERLAY_HOVER_ALPHA = 0.90
_HOVER_LEAVE_MS = 60


class OverlayWindow:
    """Panel HUD sobre el vídeo; semitransparente hasta hover."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        idle_alpha: float = OVERLAY_IDLE_ALPHA,
        hover_alpha: float = OVERLAY_HOVER_ALPHA,
        fg_color: Optional[str] = None,
        corner_radius: int = 12,
        framed: bool = True,
    ):
        self.master = master
        self._idle_alpha = float(idle_alpha)
        self._hover_alpha = float(hover_alpha)
        self._framed = framed
        self._hovered = False
        self._leave_after: Optional[str] = None
        self._geom: tuple[int, int, int, int] = (0, 0, 1, 1)

        self.win = ctk.CTkToplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.transient(master)
        self.win.attributes("-alpha", self._idle_alpha)
        try:
            self.win.attributes("-type", "splash")
        except tk.TclError:
            pass

        if framed:
            frame_kw = dict(
                fg_color=fg_color or th.OVERLAY_BG,
                corner_radius=corner_radius,
                border_width=1,
                border_color=th.OVERLAY_BORDER,
            )
        else:
            frame_kw = dict(
                fg_color="transparent",
                corner_radius=corner_radius,
                border_width=0,
            )
        self.body = ctk.CTkFrame(self.win, **frame_kw)
        self.body.pack(fill=tk.BOTH, expand=True)
        self._destroyed = False
        self.win.bind("<Enter>", self._on_pointer_enter, add="+")
        self.win.bind("<Leave>", self._on_pointer_leave, add="+")

    def register_widget(self, widget: tk.Misc) -> None:
        """Compat: el hover se detecta a nivel de ventana."""
        _ = widget

    def _on_pointer_enter(self, _event=None) -> None:
        if self._destroyed:
            return
        if self._leave_after is not None:
            try:
                self.master.after_cancel(self._leave_after)
            except tk.TclError:
                pass
            self._leave_after = None
        self._set_hovered(True)

    def _on_pointer_leave(self, _event=None) -> None:
        if self._destroyed:
            return
        if self._leave_after is not None:
            try:
                self.master.after_cancel(self._leave_after)
            except tk.TclError:
                pass
        self._leave_after = self.master.after(_HOVER_LEAVE_MS, self._check_pointer_left)

    def _check_pointer_left(self) -> None:
        self._leave_after = None
        if self._destroyed:
            return
        try:
            x, y = self.master.winfo_pointerxy()
            w = self.win.winfo_containing(x, y)
        except tk.TclError:
            return
        if w is None or not self._is_descendant(w, self.win):
            self._set_hovered(False)

    @staticmethod
    def _is_descendant(widget: tk.Misc, ancestor: tk.Misc) -> bool:
        w: Optional[tk.Misc] = widget
        while w is not None:
            if w == ancestor:
                return True
            try:
                w = w.master
            except (tk.TclError, AttributeError):
                break
        return False

    def _set_hovered(self, hovered: bool) -> None:
        if self._hovered == hovered:
            return
        self._hovered = hovered
        try:
            alpha = self._hover_alpha if hovered else self._idle_alpha
            self.win.attributes("-alpha", alpha)
        except tk.TclError:
            pass

    def set_geometry(self, x: int, y: int, width: int, height: int) -> None:
        if self._destroyed:
            return
        width = max(1, int(width))
        height = max(1, int(height))
        geom = (int(x), int(y), width, height)
        if geom == self._geom:
            return
        self._geom = geom
        try:
            self.win.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
        except tk.TclError:
            pass

    def reset_idle(self) -> None:
        """Fuerza opacidad en reposo (p. ej. al mostrar el panel)."""
        self._hovered = False
        if self._leave_after is not None:
            try:
                self.master.after_cancel(self._leave_after)
            except tk.TclError:
                pass
            self._leave_after = None
        try:
            self.win.attributes("-alpha", self._idle_alpha)
        except tk.TclError:
            pass

    def show(self) -> None:
        self.reset_idle()
        try:
            x, y, w, h = self._geom
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self.win.deiconify()
            self.win.lift()
        except tk.TclError:
            pass

    def hide(self) -> None:
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def lift(self) -> None:
        try:
            self.win.lift()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._destroyed = True
        if self._leave_after is not None:
            try:
                self.master.after_cancel(self._leave_after)
            except tk.TclError:
                pass
            self._leave_after = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
