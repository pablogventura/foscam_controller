"""Tests para detección de movimiento, zonas MD y zoom automático."""

import time

import pytest

from foscam.motion import (
    AutoZoomController,
    AutoZoomSettings,
    MotionSettings,
    ZoomState,
    load_motion_settings,
    parse_motion_detect_zones,
    parse_osd_mask_areas,
    union_box,
)


GRID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MotionDetectConfig>
<isEnable>1</isEnable>
<area0>1</area0>
<area1>2</area1>
</MotionDetectConfig>
"""

RECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MotionDetectConfig1>
<isEnable>1</isEnable>
<x0>1000</x0>
<y0>2000</y0>
<width0>3000</width0>
<height0>4000</height0>
<valid0>1</valid0>
</MotionDetectConfig1>
"""

OSD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OsdMaskArea>
<x0>0</x0>
<y0>0</y0>
<width0>5000</width0>
<height0>5000</height0>
</OsdMaskArea>
"""


def test_parse_grid_zones():
    info = parse_motion_detect_zones(GRID_XML)
    assert info.enabled is True
    assert info.grid is not None
    assert info.grid[0][0] is True
    assert info.grid[1][1] is True


def test_parse_rect_zones():
    info = parse_motion_detect_zones(RECT_XML)
    assert len(info.zones) == 1
    assert info.zones[0].x == 1000


def test_parse_osd_masks():
    masks = parse_osd_mask_areas(OSD_XML)
    assert len(masks) == 1


def test_motion_analyzer_detects_change():
    pytest.importorskip("cv2")
    import cv2
    import numpy as np
    from foscam.motion import MotionAnalyzer

    settings = MotionSettings(sensitivity=50, min_box_area_px=10)
    analyzer = MotionAnalyzer(settings)
    frame1 = np.zeros((120, 160, 3), dtype=np.uint8)
    level0, boxes0 = analyzer.analyze(frame1)
    assert level0 == 0.0
    assert boxes0 == []
    frame2 = frame1.copy()
    cv2.rectangle(frame2, (40, 30), (100, 90), (255, 255, 255), -1)
    level1, boxes1 = analyzer.analyze(frame2)
    assert level1 > 0
    assert boxes1


def test_auto_zoom_fsm_transitions():
    settings = MotionSettings(
        trigger_level=5,
        trigger_hold_sec=0.0,
        auto_zoom=AutoZoomSettings(enabled=True, return_sec=1.0, mode="digital"),
    )
    ctrl = AutoZoomController(settings)
    ctrl.has_ptz = True
    ctrl.has_optical_zoom = True
    ctrl.md_enabled = True

    ctrl.tick(20.0, [(10, 10, 50, 50)], (320, 240), gate_open=True)
    assert ctrl.state in (ZoomState.ZOOMING_IN, ZoomState.ZOOMED_HOLD)

    ctrl.state = ZoomState.ZOOMED_HOLD
    ctrl._idle_since = time.time() - 10
    ctrl.tick(0.0, [], (320, 240), gate_open=True)
    assert ctrl.state == ZoomState.RETURNING


def test_load_motion_settings_merge():
    prefs = {"motion": {"sensitivity": 40, "auto_zoom": {"enabled": True}}}
    s = load_motion_settings(prefs, {"sensitivity": 55})
    assert s.sensitivity == 55
    assert s.auto_zoom.enabled is True


def test_union_box():
    boxes = [(0, 0, 10, 10), (5, 5, 20, 15)]
    assert union_box(boxes) == (0, 0, 20, 15)


def test_delete_profile():
    s = MotionSettings()
    s.save_profile("test")
    assert "test" in s.profiles
    assert s.delete_profile("test") is True
    assert "test" not in s.profiles


def test_filter_boxes_in_md_zones():
    from foscam.motion import MotionDetectInfo, ZoneRect, filter_boxes_in_md_zones

    info = MotionDetectInfo(zones=[ZoneRect(0, 0, 5000, 5000, normalized=True)])
    boxes = [(10, 10, 50, 50), (900, 900, 950, 950)]
    filtered = filter_boxes_in_md_zones(boxes, info, 1000, 1000)
    assert len(filtered) == 1
    assert filtered[0] == boxes[0]


def test_point_in_md_grid():
    from foscam.motion import MotionDetectInfo, point_in_md_zones

    grid = [[False] * 10 for _ in range(10)]
    grid[5][5] = True
    info = MotionDetectInfo(grid=grid)
    assert point_in_md_zones(550, 550, info, 1000, 1000) is True
    assert point_in_md_zones(50, 50, info, 1000, 1000) is False
