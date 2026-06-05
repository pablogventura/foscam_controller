"""Detección de movimiento, overlays y zoom automático para el visor Foscam."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - tests may mock
    cv2 = None
    np = None

MOTION_PRESETS: Dict[str, Dict[str, Any]] = {
    "sensitive": {"sensitivity": 65.0, "trigger_level": 10.0, "min_box_area_px": 80},
    "normal": {"sensitivity": 50.0, "trigger_level": 15.0, "min_box_area_px": 120},
    "strict": {"sensitivity": 35.0, "trigger_level": 25.0, "min_box_area_px": 200},
}

ANALYSIS_HEIGHT = 120
METER_EMA_ALPHA = 0.25


def _hex_to_bgr(color: str) -> Tuple[int, int, int]:
    c = (color or "#ffffff").lstrip("#")
    if len(c) == 6:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return b, g, r
    return 60, 76, 231


def _expand_path(path: str) -> Path:
    return Path(path).expanduser()


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dict(out[key], val)
        else:
            out[key] = val
    return out


def _dataclass_from_dict(cls, data: Optional[Dict[str, Any]]):
    if not data:
        return cls()
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        if is_dataclass(f.type) or (hasattr(f.type, "__origin__") is False and hasattr(cls, f.name)):
            inner = getattr(cls, f.name, None)
        inner_cls = None
        if is_dataclass(f.default_factory) if callable(f.default_factory) else False:
            pass
        # Resolve nested dataclass by field default_factory
        try:
            sample = cls()
            inner_obj = getattr(sample, f.name, None)
            if is_dataclass(inner_obj) and isinstance(val, dict):
                kwargs[f.name] = _dataclass_from_dict(type(inner_obj), val)
                continue
        except TypeError:
            pass
        if f.name == "profiles" and isinstance(val, dict):
            kwargs[f.name] = val
        elif f.name == "active_profile":
            kwargs[f.name] = val
        else:
            kwargs[f.name] = val
    return cls(**kwargs)


@dataclass
class AutoZoomSettings:
    enabled: bool = False
    mode: str = "auto"
    return_sec: float = 5.0
    max_digital: float = 3.0
    target_fill_ratio: float = 0.4
    zoom_in_speed: float = 1.0
    return_speed: float = 1.5
    return_use_ptz_reset: bool = True
    return_use_zoom_out: bool = True
    pause_on_manual_sec: float = 10.0
    show_state_badge: bool = True
    ptz_speed: int = 2
    ptz_pulse_ms: int = 200
    ptz_cooldown_ms: int = 400
    max_optical_steps: int = 5
    optical_step_cooldown_ms: int = 500
    pan_deadzone_pct: float = 12.0


@dataclass
class SnapshotSettings:
    enabled: bool = False
    dir: str = "~/Pictures/foscam-motion"


@dataclass
class HeatmapSettings:
    enabled: bool = False
    decay_sec: float = 120.0


@dataclass
class MotionSettings:
    show_live_overlay: bool = False
    show_configured_zones: bool = False
    live_overlay_style: str = "boxes"
    live_overlay_color: str = "#e74c3c"
    live_overlay_line_width: int = 2
    live_overlay_fill_alpha: float = 0.15
    live_show_centroid: bool = False
    live_show_level_label: bool = False
    zones_overlay_color: str = "#3498db"
    zones_overlay_alpha: float = 0.25
    zones_overlay_style: str = "grid"
    zones_refresh_sec: float = 30.0
    zones_config_source: str = "auto"
    sensitivity: float = 50.0
    trigger_level: float = 15.0
    trigger_hold_sec: float = 0.5
    min_box_area_px: int = 120
    crop_margin_pct: float = 15.0
    smoothing_alpha: float = 0.35
    morph_kernel: int = 3
    analysis_width: int = 160
    ignore_zones_outside_md: bool = False
    require_audio_gate: bool = False
    ignore_camera_md_disabled: bool = False
    debug_fsm_overlay: bool = False
    show_legend: bool = True
    flash_alarm_border: bool = False
    alarm_border_color: str = "#f39c12"
    privacy_mask_overlay: bool = False
    preset: str = "normal"
    active_profile: Optional[str] = None
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    auto_zoom: AutoZoomSettings = field(default_factory=AutoZoomSettings)
    snapshot_on_motion: SnapshotSettings = field(default_factory=SnapshotSettings)
    heatmap: HeatmapSettings = field(default_factory=HeatmapSettings)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "MotionSettings":
        if not data:
            return cls()
        d = dict(data)
        az = d.pop("auto_zoom", None)
        snap = d.pop("snapshot_on_motion", None)
        hm = d.pop("heatmap", None)
        profiles = d.pop("profiles", None)
        active = d.pop("active_profile", None)
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        obj = cls(**kwargs)
        if isinstance(az, dict):
            for f in fields(AutoZoomSettings):
                if f.name in az:
                    setattr(obj.auto_zoom, f.name, az[f.name])
        if isinstance(snap, dict):
            for f in fields(SnapshotSettings):
                if f.name in snap:
                    setattr(obj.snapshot_on_motion, f.name, snap[f.name])
        if isinstance(hm, dict):
            for f in fields(HeatmapSettings):
                if f.name in hm:
                    setattr(obj.heatmap, f.name, hm[f.name])
        if isinstance(profiles, dict):
            obj.profiles = profiles
        if active is not None:
            obj.active_profile = active
        return obj

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def apply_preset(self, name: str) -> None:
        key = name.lower()
        if key not in MOTION_PRESETS:
            return
        self.preset = key
        for k, v in MOTION_PRESETS[key].items():
            setattr(self, k, v)

    def apply_profile(self, name: Optional[str]) -> bool:
        if not name or name not in self.profiles:
            return False
        merged = _merge_dict(self.to_dict(), self.profiles[name])
        new = MotionSettings.from_dict(merged)
        new.profiles = self.profiles
        new.active_profile = name
        self.__dict__.update(new.__dict__)
        return True

    def save_profile(self, name: str) -> None:
        payload = self.to_dict()
        payload.pop("profiles", None)
        payload.pop("active_profile", None)
        self.profiles[name] = payload
        self.active_profile = name

    def delete_profile(self, name: str) -> bool:
        if name not in self.profiles:
            return False
        del self.profiles[name]
        if self.active_profile == name:
            self.active_profile = None
        return True


def load_motion_settings(prefs: Dict[str, Any], cli_overrides: Optional[Dict[str, Any]] = None) -> MotionSettings:
    base = MotionSettings.from_dict(prefs.get("motion"))
    if cli_overrides:
        merged = _merge_dict(base.to_dict(), cli_overrides)
        base = MotionSettings.from_dict(merged)
    if base.active_profile:
        base.apply_profile(base.active_profile)
    return base


@dataclass
class ZoneRect:
    x: float
    y: float
    w: float
    h: float
    normalized: bool = True  # 0-10000 or 0-1 fraction


@dataclass
class MotionDetectInfo:
    zones: List[ZoneRect] = field(default_factory=list)
    grid: Optional[List[List[bool]]] = None
    enabled: bool = True
    raw: Dict[str, str] = field(default_factory=dict)


def _xml_fields(xml_text: str) -> Dict[str, str]:
    if not xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    out: Dict[str, str] = {}
    for el in root.iter():
        tag = (el.tag or "").split("}")[-1]
        text = (el.text or "").strip()
        if text:
            out[tag] = text
    return out


def parse_motion_detect_zones(xml_text: str) -> MotionDetectInfo:
    fields_map = _xml_fields(xml_text)
    info = MotionDetectInfo(raw=fields_map)
    enable_val = fields_map.get("isEnable", fields_map.get("enable", "1"))
    info.enabled = enable_val not in ("0", "false", "no", "off")

    rects: List[ZoneRect] = []
    idx = 0
    while f"x{idx}" in fields_map or f"X{idx}" in fields_map:
        prefix = "" if f"x{idx}" in fields_map else ""
        x = float(fields_map.get(f"x{idx}", fields_map.get(f"X{idx}", 0)))
        y = float(fields_map.get(f"y{idx}", fields_map.get(f"Y{idx}", 0)))
        w = float(fields_map.get(f"width{idx}", fields_map.get(f"Width{idx}", 0)))
        h = float(fields_map.get(f"height{idx}", fields_map.get(f"Height{idx}", 0)))
        valid = fields_map.get(f"valid{idx}", "1")
        if valid not in ("0", "false"):
            rects.append(ZoneRect(x, y, w, h, normalized=True))
        idx += 1
    if rects:
        info.zones = rects
        return info

    grid: List[List[bool]] = []
    for row in range(10):
        key = f"area{row}"
        if key not in fields_map:
            break
        try:
            mask = int(fields_map[key])
        except ValueError:
            mask = 0
        row_cells = []
        for col in range(10):
            row_cells.append(bool(mask & (1 << col)))
        grid.append(row_cells)
    if grid:
        info.grid = grid
    return info


def parse_osd_mask_areas(xml_text: str) -> List[ZoneRect]:
    fields_map = _xml_fields(xml_text)
    rects: List[ZoneRect] = []
    idx = 0
    while f"x{idx}" in fields_map:
        x = float(fields_map.get(f"x{idx}", 0))
        y = float(fields_map.get(f"y{idx}", 0))
        w = float(fields_map.get(f"width{idx}", 0))
        h = float(fields_map.get(f"height{idx}", 0))
        rects.append(ZoneRect(x, y, w, h, normalized=True))
        idx += 1
    return rects


def zone_to_pixel_rect(zone: ZoneRect, width: int, height: int) -> Tuple[int, int, int, int]:
    if zone.normalized and max(zone.x, zone.y, zone.w, zone.h) > 1.5:
        scale_x = width / 10000.0
        scale_y = height / 10000.0
        x1 = int(zone.x * scale_x)
        y1 = int(zone.y * scale_y)
        x2 = int((zone.x + zone.w) * scale_x)
        y2 = int((zone.y + zone.h) * scale_y)
    else:
        x1 = int(zone.x * width)
        y1 = int(zone.y * height)
        x2 = int((zone.x + zone.w) * width)
        y2 = int((zone.y + zone.h) * height)
    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(x1 + 1, min(width, x2))
    y2 = max(y1 + 1, min(height, y2))
    return x1, y1, x2, y2


def map_box_to_display(
    box: Tuple[int, int, int, int],
    src_size: Tuple[int, int],
    display_size: Tuple[int, int],
    letterbox_size: Tuple[int, int],
) -> Tuple[int, int, int, int]:
    sw, sh = src_size
    dw, dh = letterbox_size
    x1, y1, x2, y2 = box
    sx = dw / max(1, sw)
    sy = dh / max(1, sh)
    return int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)


class MotionAnalyzer:
    def __init__(self, settings: MotionSettings):
        self.settings = settings
        self._prev_gray = None
        self._level_ema = 0.0
        self._boxes_ema: Optional[Tuple[int, int, int, int]] = None

    def reset(self) -> None:
        self._prev_gray = None
        self._level_ema = 0.0
        self._boxes_ema = None

    def _analysis_size(self, frame) -> Tuple[int, int]:
        h, w = frame.shape[:2]
        aw = max(32, int(self.settings.analysis_width))
        ah = max(24, int(aw * h / max(1, w)))
        return aw, ah

    def analyze(self, frame) -> Tuple[float, List[Tuple[int, int, int, int]]]:
        if cv2 is None or frame is None:
            return 0.0, []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        aw, ah = self._analysis_size(frame)
        small = cv2.resize(gray, (aw, ah), interpolation=cv2.INTER_AREA)
        if self._prev_gray is None:
            self._prev_gray = small
            return 0.0, []
        diff = cv2.absdiff(self._prev_gray, small)
        self._prev_gray = small
        raw = float(cv2.mean(diff)[0])
        sens = max(1.0, self.settings.sensitivity)
        threshold = max(2.0, 30.0 - sens * 0.25)
        instant = max(0.0, min(100.0, (raw / 25.0) * 100.0))
        alpha = max(0.05, min(0.95, self.settings.smoothing_alpha))
        self._level_ema = alpha * instant + (1.0 - alpha) * self._level_ema

        _, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        k = int(self.settings.morph_kernel)
        if k > 0:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        fh, fw = frame.shape[:2]
        scale_x = fw / aw
        scale_y = fh / ah
        min_area = max(1, int(self.settings.min_box_area_px))
        boxes: List[Tuple[int, int, int, int]] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            x1 = int(x * scale_x)
            y1 = int(y * scale_y)
            x2 = int((x + bw) * scale_x)
            y2 = int((y + bh) * scale_y)
            boxes.append((x1, y1, x2, y2))

        if boxes and self._boxes_ema is not None:
            bx1 = min(b[0] for b in boxes)
            by1 = min(b[1] for b in boxes)
            bx2 = max(b[2] for b in boxes)
            by2 = max(b[3] for b in boxes)
            ex1, ey1, ex2, ey2 = self._boxes_ema
            bx1 = int(alpha * bx1 + (1 - alpha) * ex1)
            by1 = int(alpha * by1 + (1 - alpha) * ey1)
            bx2 = int(alpha * bx2 + (1 - alpha) * ex2)
            by2 = int(alpha * by2 + (1 - alpha) * ey2)
            boxes = [(bx1, by1, bx2, by2)]
        elif boxes:
            bx1 = min(b[0] for b in boxes)
            by1 = min(b[1] for b in boxes)
            bx2 = max(b[2] for b in boxes)
            by2 = max(b[3] for b in boxes)
            boxes = [(bx1, by1, bx2, by2)]
            self._boxes_ema = boxes[0]
        elif self._boxes_ema is not None:
            self._boxes_ema = None

        if boxes:
            self._boxes_ema = boxes[0]
        return self._level_ema, boxes

    def analyze_with_zones(
        self,
        frame,
        md_info: Optional[MotionDetectInfo],
    ) -> Tuple[float, List[Tuple[int, int, int, int]]]:
        level, boxes = self.analyze(frame)
        if self.settings.ignore_zones_outside_md and md_info and frame is not None:
            fh, fw = frame.shape[:2]
            boxes = filter_boxes_in_md_zones(boxes, md_info, fw, fh)
            if not boxes:
                level = min(level, max(0.0, self.settings.trigger_level - 1.0))
        return level, boxes

    @property
    def level(self) -> float:
        return self._level_ema


def point_in_md_zones(px: float, py: float, md_info: MotionDetectInfo, width: int, height: int) -> bool:
    if md_info.grid:
        col = int(px / max(1, width) * 10)
        row = int(py / max(1, height) * 10)
        col = max(0, min(9, col))
        row = max(0, min(9, row))
        if row < len(md_info.grid) and col < len(md_info.grid[row]):
            return bool(md_info.grid[row][col])
        return False
    if md_info.zones:
        for zone in md_info.zones:
            x1, y1, x2, y2 = zone_to_pixel_rect(zone, width, height)
            if x1 <= px <= x2 and y1 <= py <= y2:
                return True
        return False
    return True


def filter_boxes_in_md_zones(
    boxes: List[Tuple[int, int, int, int]],
    md_info: MotionDetectInfo,
    width: int,
    height: int,
) -> List[Tuple[int, int, int, int]]:
    out = []
    for x1, y1, x2, y2 in boxes:
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        if point_in_md_zones(cx, cy, md_info, width, height):
            out.append((x1, y1, x2, y2))
    return out


class HeatmapAccumulator:
    def __init__(self, settings: HeatmapSettings):
        self.settings = settings
        self._grid: Optional[np.ndarray] = None
        self._last_ts = time.time()

    def reset(self) -> None:
        self._grid = None

    def update(self, frame_shape: Tuple[int, int, int], boxes: List[Tuple[int, int, int, int]]) -> None:
        if np is None or not self.settings.enabled:
            return
        h, w = frame_shape[:2]
        if self._grid is None or self._grid.shape != (h, w):
            self._grid = np.zeros((h, w), dtype=np.float32)
        now = time.time()
        dt = max(0.0, now - self._last_ts)
        self._last_ts = now
        decay = dt / max(1.0, self.settings.decay_sec)
        self._grid *= max(0.0, 1.0 - decay)
        for x1, y1, x2, y2 in boxes:
            self._grid[y1:y2, x1:x2] += 1.0

    def overlay(self, frame, alpha: float = 0.35):
        if cv2 is None or self._grid is None or np is None:
            return frame
        fh, fw = frame.shape[:2]
        grid = self._grid
        if grid.shape[:2] != (fh, fw):
            grid = cv2.resize(grid, (fw, fh), interpolation=cv2.INTER_LINEAR)
        norm = np.clip(grid / max(1.0, float(grid.max())), 0, 1)
        heat = (norm * 255).astype(np.uint8)
        heat_color = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
        return cv2.addWeighted(frame, 1.0 - alpha, heat_color, alpha, 0)


def union_box(boxes: List[Tuple[int, int, int, int]]) -> Optional[Tuple[int, int, int, int]]:
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def expand_box(box: Tuple[int, int, int, int], margin_pct: float, width: int, height: int):
    x1, y1, x2, y2 = box
    bw = x2 - x1
    bh = y2 - y1
    mx = int(bw * margin_pct / 100.0)
    my = int(bh * margin_pct / 100.0)
    return (
        max(0, x1 - mx),
        max(0, y1 - my),
        min(width, x2 + mx),
        min(height, y2 + my),
    )


def draw_motion_overlays(
    frame,
    settings: MotionSettings,
    boxes: List[Tuple[int, int, int, int]],
    md_info: Optional[MotionDetectInfo],
    privacy_masks: Optional[List[ZoneRect]],
    level: float,
    src_size: Tuple[int, int],
    letterbox_size: Tuple[int, int],
    debug_text: Optional[str] = None,
    alarm_flash: bool = False,
):
    if cv2 is None:
        return frame
    out = frame
    fh, fw = out.shape[:2]

    if settings.privacy_mask_overlay and privacy_masks:
        for zone in privacy_masks:
            x1, y1, x2, y2 = zone_to_pixel_rect(zone, fw, fh)
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 0), -1)

    if settings.show_configured_zones and md_info:
        color = _hex_to_bgr(settings.zones_overlay_color)
        alpha = max(0.0, min(1.0, settings.zones_overlay_alpha))
        overlay = out.copy()
        if md_info.grid:
            cell_w = fw / 10.0
            cell_h = fh / 10.0
            for row, row_cells in enumerate(md_info.grid):
                for col, active in enumerate(row_cells):
                    if not active:
                        continue
                    x1 = int(col * cell_w)
                    y1 = int(row * cell_h)
                    x2 = int((col + 1) * cell_w)
                    y2 = int((row + 1) * cell_h)
                    if settings.zones_overlay_style == "fill":
                        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
                    else:
                        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 1)
        for zone in md_info.zones:
            x1, y1, x2, y2 = zone_to_pixel_rect(zone, fw, fh)
            if settings.zones_overlay_style == "fill":
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            else:
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)
        out = cv2.addWeighted(overlay, alpha, out, 1.0 - alpha, 0)

    if settings.show_live_overlay and boxes:
        color = _hex_to_bgr(settings.live_overlay_color)
        lw = max(1, int(settings.live_overlay_line_width))
        for box in boxes:
            x1, y1, x2, y2 = map_box_to_display(box, src_size, (fw, fh), letterbox_size)
            if settings.live_overlay_style == "filled":
                sub = out.copy()
                cv2.rectangle(sub, (x1, y1), (x2, y2), color, -1)
                fa = settings.live_overlay_fill_alpha
                out = cv2.addWeighted(sub, fa, out, 1.0 - fa, 0)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, lw)
            elif settings.live_overlay_style == "crosshair":
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                cv2.drawMarker(out, (cx, cy), color, cv2.MARKER_CROSS, lw * 6, lw)
            else:
                cv2.rectangle(out, (x1, y1), (x2, y2), color, lw)
            if settings.live_show_centroid:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                cv2.circle(out, (cx, cy), 4, color, -1)
            if settings.live_show_level_label:
                cv2.putText(
                    out, f"{level:.0f}%", (x1, max(12, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA,
                )

    if settings.flash_alarm_border and alarm_flash:
        bc = _hex_to_bgr(settings.alarm_border_color)
        cv2.rectangle(out, (0, 0), (fw - 1, fh - 1), bc, 4)

    if settings.debug_fsm_overlay and debug_text:
        cv2.rectangle(out, (4, 4), (min(fw - 4, 420), 28), (0, 0, 0), -1)
        cv2.putText(out, debug_text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

    if settings.show_legend and (settings.show_live_overlay or settings.show_configured_zones):
        legend = "Rojo=movimiento  Azul=zonas MD"
        cv2.putText(
            out, legend, (8, fh - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA,
        )

    return out


class ZoomState(str, Enum):
    NORMAL = "normal"
    ZOOMING_IN = "zooming_in"
    ZOOMED_HOLD = "zoomed_hold"
    RETURNING = "returning"


@dataclass
class AutoZoomAction:
    ptz_commands: List[Tuple[str, Optional[Dict[str, Any]]]] = field(default_factory=list)
    digital_crop: Optional[Tuple[int, int, int, int]] = None
    snapshot: bool = False
    state_label: str = "normal"


class AutoZoomController:
    STATE_LABELS = {
        ZoomState.NORMAL: "normal",
        ZoomState.ZOOMING_IN: "acercando",
        ZoomState.ZOOMED_HOLD: "fijado",
        ZoomState.RETURNING: "volviendo",
    }

    def __init__(self, settings: MotionSettings):
        self.settings = settings
        self.state = ZoomState.NORMAL
        self.has_ptz = False
        self.has_optical_zoom = False
        self.md_enabled = True
        self._motion_since: Optional[float] = None
        self._idle_since: Optional[float] = None
        self._manual_pause_until = 0.0
        self._last_tick = time.time()
        self._digital_factor = 1.0
        self._target_factor = 1.0
        self._optical_steps = 0
        self._last_ptz_ts = 0.0
        self._last_optical_ts = 0.0
        self._crop_rect: Optional[Tuple[int, int, int, int]] = None
        self._snapshot_fired = False
        self._frame_size: Tuple[int, int] = (1, 1)

    def reset(self) -> None:
        self.state = ZoomState.NORMAL
        self._motion_since = None
        self._idle_since = None
        self._digital_factor = 1.0
        self._target_factor = 1.0
        self._optical_steps = 0
        self._crop_rect = None
        self._snapshot_fired = False

    def notify_manual_ptz(self) -> None:
        az = self.settings.auto_zoom
        self._manual_pause_until = time.time() + az.pause_on_manual_sec
        self.state = ZoomState.NORMAL
        self._digital_factor = 1.0
        self._target_factor = 1.0
        self._crop_rect = None

    def _effective_mode(self) -> str:
        az = self.settings.auto_zoom
        mode = az.mode
        if mode == "auto":
            if self.has_ptz and self.has_optical_zoom:
                return "ptz"
            if self.has_ptz:
                return "ptz_pan_digital_zoom"
            return "digital"
        return mode

    def tick(
        self,
        level: float,
        boxes: List[Tuple[int, int, int, int]],
        frame_size: Tuple[int, int],
        gate_open: bool,
    ) -> AutoZoomAction:
        az = self.settings.auto_zoom
        action = AutoZoomAction(state_label=self.STATE_LABELS.get(self.state, "normal"))
        if not az.enabled or time.time() < self._manual_pause_until:
            action.digital_crop = None
            return action
        if not self.md_enabled and not self.settings.ignore_camera_md_disabled:
            return action
        if self.settings.require_audio_gate and not gate_open:
            return action

        fw, fh = frame_size
        self._frame_size = frame_size
        now = time.time()
        dt = max(0.001, now - self._last_tick)
        self._last_tick = now
        has_boxes = bool(boxes)
        if self.state in (ZoomState.ZOOMING_IN, ZoomState.ZOOMED_HOLD):
            moving = has_boxes
        else:
            moving = has_boxes and level >= self.settings.trigger_level

        if moving:
            self._idle_since = None
            if self._motion_since is None:
                self._motion_since = now
        else:
            self._motion_since = None
            if self._idle_since is None:
                self._idle_since = now

        mode = self._effective_mode()

        if self.state == ZoomState.NORMAL:
            action.digital_crop = None
            if moving and self._motion_since and (now - self._motion_since) >= self.settings.trigger_hold_sec:
                self.state = ZoomState.ZOOMING_IN
                self._snapshot_fired = False
                self._optical_steps = 0
                ubox = union_box(boxes)
                if ubox:
                    self._update_target_crop(ubox, fw, fh, az)

        if self.state == ZoomState.ZOOMING_IN:
            if not moving:
                self.state = ZoomState.RETURNING
            else:
                ubox = union_box(boxes)
                if ubox:
                    self._update_target_crop(ubox, fw, fh, az)
                self._digital_factor = min(
                    az.max_digital,
                    self._digital_factor + az.zoom_in_speed * dt * 1.2,
                )
                self._apply_ptz_toward_box(boxes, fw, fh, mode, action, zoom_in=True)
                if self._digital_factor >= az.max_digital or self._reached_target_fill(boxes, fw, fh, az):
                    self.state = ZoomState.ZOOMED_HOLD
                if not self._snapshot_fired and self.settings.snapshot_on_motion.enabled:
                    action.snapshot = True
                    self._snapshot_fired = True

        elif self.state == ZoomState.ZOOMED_HOLD:
            if not moving and self._idle_since and (now - self._idle_since) >= az.return_sec:
                self.state = ZoomState.RETURNING
            elif moving:
                ubox = union_box(boxes)
                if ubox:
                    self._update_target_crop(ubox, fw, fh, az)
                self._apply_ptz_toward_box(boxes, fw, fh, mode, action, zoom_in=False)

        elif self.state == ZoomState.RETURNING:
            self._digital_factor = max(1.0, self._digital_factor - az.return_speed * dt * 0.8)
            if mode.startswith("ptz") and az.return_use_zoom_out:
                self._maybe_queue(action, "zoomOut", None, az.optical_step_cooldown_ms)
            if self._digital_factor <= 1.01:
                self._digital_factor = 1.0
                self._crop_rect = None
                if mode.startswith("ptz"):
                    self._maybe_queue(action, "zoomStop", None, 0)
                    if az.return_use_ptz_reset and self.has_ptz:
                        self._maybe_queue(action, "ptzReset", None, 0)
                self.state = ZoomState.NORMAL
                self._optical_steps = 0

        action.digital_crop = self._current_crop(fw, fh)
        action.state_label = self.STATE_LABELS.get(self.state, "normal")
        return action

    def _box_fill_in_frame(
        self,
        box: Tuple[int, int, int, int],
        crop: Optional[Tuple[int, int, int, int]],
        fw: int,
        fh: int,
    ) -> float:
        x1, y1, x2, y2 = box
        if crop is None:
            return ((x2 - x1) * (y2 - y1)) / max(1, fw * fh)
        cx1, cy1, cx2, cy2 = crop
        ix1, iy1 = max(x1, cx1), max(y1, cy1)
        ix2, iy2 = min(x2, cx2), min(y2, cy2)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        return ((ix2 - ix1) * (iy2 - iy1)) / max(1, fw * fh)

    def _reached_target_fill(self, boxes, fw, fh, az) -> bool:
        ubox = union_box(boxes)
        if not ubox:
            return False
        mode = self._effective_mode()
        if mode in ("digital", "ptz_pan_digital_zoom"):
            return self._digital_factor >= az.max_digital - 0.02
        return self._box_fill_in_frame(ubox, None, fw, fh) >= az.target_fill_ratio

    def _update_target_crop(self, box, fw, fh, az) -> None:
        x1, y1, x2, y2 = expand_box(box, self.settings.crop_margin_pct, fw, fh)
        self._crop_rect = (x1, y1, x2, y2)

    def _current_crop(self, fw: int, fh: int) -> Optional[Tuple[int, int, int, int]]:
        if self._digital_factor <= 1.001 or not self._crop_rect:
            return None
        x1, y1, x2, y2 = self._crop_rect
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        bw = (x2 - x1) / self._digital_factor
        bh = (y2 - y1) / self._digital_factor
        nx1 = int(max(0, cx - bw / 2))
        ny1 = int(max(0, cy - bh / 2))
        nx2 = int(min(fw, cx + bw / 2))
        ny2 = int(min(fh, cy + bh / 2))
        if nx2 - nx1 < 8 or ny2 - ny1 < 8:
            return None
        return nx1, ny1, nx2, ny2

    def _apply_ptz_toward_box(self, boxes, fw, fh, mode, action, zoom_in: bool) -> None:
        if not mode.startswith("ptz") or not self.has_ptz:
            return
        az = self.settings.auto_zoom
        ubox = union_box(boxes)
        if not ubox:
            return
        x1, y1, x2, y2 = ubox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        dead = az.pan_deadzone_pct / 100.0
        left = fw * dead
        right = fw * (1 - dead)
        top = fh * dead
        bottom = fh * (1 - dead)
        if cx < left:
            self._maybe_queue(action, "ptzMoveLeft", None, az.ptz_cooldown_ms)
        elif cx > right:
            self._maybe_queue(action, "ptzMoveRight", None, az.ptz_cooldown_ms)
        if cy < top:
            self._maybe_queue(action, "ptzMoveUp", None, az.ptz_cooldown_ms)
        elif cy > bottom:
            self._maybe_queue(action, "ptzMoveDown", None, az.ptz_cooldown_ms)
        if zoom_in and self.has_optical_zoom and mode == "ptz":
            if self._optical_steps < az.max_optical_steps:
                self._maybe_queue(action, "zoomIn", None, az.optical_step_cooldown_ms)
                self._optical_steps += 1
        self._maybe_queue(action, "ptzStopRun", None, az.ptz_pulse_ms)

    def _maybe_queue(self, action: AutoZoomAction, cmd: str, params, cooldown_ms: float) -> None:
        now = time.time()
        if (now - self._last_ptz_ts) * 1000.0 < cooldown_ms and cooldown_ms > 0:
            return
        action.ptz_commands.append((cmd, params))
        self._last_ptz_ts = now


def apply_digital_crop(frame, crop: Optional[Tuple[int, int, int, int]]):
    if cv2 is None or crop is None or frame is None:
        return frame
    x1, y1, x2, y2 = crop
    h, w = frame.shape[:2]
    x1 = max(0, min(w - 2, x1))
    y1 = max(0, min(h - 2, y1))
    x2 = max(x1 + 2, min(w, x2))
    y2 = max(y1 + 2, min(h, y2))
    cropped = frame[y1:y2, x1:x2]
    return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)


def save_motion_to_prefs(prefs_path: Path, motion: MotionSettings, **extra) -> None:
    try:
        data: Dict[str, Any] = {}
        if prefs_path.is_file():
            with open(prefs_path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                data = existing
        data["motion"] = motion.to_dict()
        data.update(extra)
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
