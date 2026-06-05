"""Pacing de refresco y decimación de motion para el visor (funciones puras)."""

from __future__ import annotations

DISPLAY_MAX_FPS = 60
DISPLAY_IDLE_POLL_MS = 16
DISPLAY_MIN_INTERVAL_MS = max(1, int(1000 / DISPLAY_MAX_FPS))
MOTION_ANALYSIS_EVERY_N_FRAMES = 3
MOTION_ANALYSIS_MIN_INTERVAL_SEC = 0.066
READER_QUEUE_FULL_SLEEP_SEC = 0.003


def next_display_delay_ms(queue_depth: int, elapsed_since_paint_ms: float) -> int:
    """Retardo hasta el próximo tick de pintado (ms)."""
    if queue_depth > 0:
        return 0
    if elapsed_since_paint_ms > 0 and elapsed_since_paint_ms < DISPLAY_MIN_INTERVAL_MS:
        return max(0, int(DISPLAY_MIN_INTERVAL_MS - elapsed_since_paint_ms))
    return DISPLAY_IDLE_POLL_MS


def motion_analysis_needed(
    *,
    show_live_overlay: bool,
    show_configured_zones: bool,
    auto_zoom_enabled: bool,
    heatmap_enabled: bool,
    snapshot_enabled: bool,
    debug_fsm_overlay: bool,
) -> bool:
    """True si algún consumidor necesita análisis de movimiento."""
    return any((
        show_live_overlay,
        show_configured_zones,
        auto_zoom_enabled,
        heatmap_enabled,
        snapshot_enabled,
        debug_fsm_overlay,
    ))


def should_run_motion_analysis(
    *,
    needed: bool,
    frame_index: int,
    last_analysis_monotonic: float,
    now_monotonic: float,
    every_n_frames: int = MOTION_ANALYSIS_EVERY_N_FRAMES,
    min_interval_sec: float = MOTION_ANALYSIS_MIN_INTERVAL_SEC,
) -> bool:
    """Decide si analizar movimiento en este frame."""
    if not needed:
        return False
    if frame_index % every_n_frames == 0:
        return True
    return (now_monotonic - last_analysis_monotonic) >= min_interval_sec


def visual_overlays_needed(
    *,
    show_live_overlay: bool,
    show_configured_zones: bool,
    privacy_mask_overlay: bool,
    has_privacy_masks: bool,
    flash_alarm_border: bool,
    alarm_active: bool,
    debug_fsm_overlay: bool,
) -> bool:
    """True si hace falta draw_motion_overlays / heatmap en el frame."""
    if show_live_overlay or show_configured_zones:
        return True
    if privacy_mask_overlay and has_privacy_masks:
        return True
    if flash_alarm_border and alarm_active:
        return True
    if debug_fsm_overlay:
        return True
    return False
