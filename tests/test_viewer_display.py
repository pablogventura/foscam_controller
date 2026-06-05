"""Tests de pacing y decimación del visor."""

import time

from foscam.display_pacing import (
    DISPLAY_IDLE_POLL_MS,
    DISPLAY_MIN_INTERVAL_MS,
    motion_analysis_needed,
    next_display_delay_ms,
    should_run_motion_analysis,
    visual_overlays_needed,
)


def test_next_display_delay_drains_queue_immediately():
    assert next_display_delay_ms(1, 0.0) == 0
    assert next_display_delay_ms(2, 0.0) == 0


def test_next_display_delay_respects_max_fps_cap():
    delay = next_display_delay_ms(0, DISPLAY_MIN_INTERVAL_MS - 1)
    assert delay == 1
    delay = next_display_delay_ms(0, DISPLAY_MIN_INTERVAL_MS)
    assert delay == DISPLAY_IDLE_POLL_MS


def test_next_display_delay_idle_poll():
    assert next_display_delay_ms(0, 0.0) == DISPLAY_IDLE_POLL_MS


def test_motion_analysis_needed_all_off():
    assert motion_analysis_needed(
        show_live_overlay=False,
        show_configured_zones=False,
        auto_zoom_enabled=False,
        heatmap_enabled=False,
        snapshot_enabled=False,
        debug_fsm_overlay=False,
    ) is False


def test_motion_analysis_needed_auto_zoom():
    assert motion_analysis_needed(
        show_live_overlay=False,
        show_configured_zones=False,
        auto_zoom_enabled=True,
        heatmap_enabled=False,
        snapshot_enabled=False,
        debug_fsm_overlay=False,
    ) is True


def test_should_run_motion_analysis_skips_when_not_needed():
    now = time.monotonic()
    assert should_run_motion_analysis(
        needed=False,
        frame_index=3,
        last_analysis_monotonic=0.0,
        now_monotonic=now,
    ) is False


def test_should_run_motion_analysis_every_n_frames():
    now = time.monotonic()
    assert should_run_motion_analysis(
        needed=True,
        frame_index=3,
        last_analysis_monotonic=now,
        now_monotonic=now,
    ) is True
    assert should_run_motion_analysis(
        needed=True,
        frame_index=1,
        last_analysis_monotonic=now,
        now_monotonic=now,
    ) is False


def test_should_run_motion_analysis_min_interval():
    now = time.monotonic()
    assert should_run_motion_analysis(
        needed=True,
        frame_index=1,
        last_analysis_monotonic=now - 0.1,
        now_monotonic=now,
    ) is True


def test_visual_overlays_needed_alarm_border():
    assert visual_overlays_needed(
        show_live_overlay=False,
        show_configured_zones=False,
        privacy_mask_overlay=False,
        has_privacy_masks=False,
        flash_alarm_border=True,
        alarm_active=True,
        debug_fsm_overlay=False,
    ) is True
    assert visual_overlays_needed(
        show_live_overlay=False,
        show_configured_zones=False,
        privacy_mask_overlay=False,
        has_privacy_masks=False,
        flash_alarm_border=True,
        alarm_active=False,
        debug_fsm_overlay=False,
    ) is False
