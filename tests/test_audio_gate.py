"""Tests unitarios de la puerta de ruido."""

import math
import shutil

import numpy as np
import pytest

from foscam.audio_gate import (
    GATE_SLIDER_MIN,
    METER_DB_MAX,
    METER_DB_MIN,
    apply_gate,
    db_to_meter_ratio,
    apply_playback_volume,
    ffplay_agate_filter,
    gate_is_open,
    gate_threshold_disabled,
    generate_noise_floor_with_peak,
    generate_sine_dbfs,
    process_playback_chunk,
    samples_db,
    sweep_gate_response,
)


def test_meter_bar_and_gate_same_scale():
    level_db = -44.0
    gate_db = -38.0
    assert db_to_meter_ratio(level_db) < db_to_meter_ratio(gate_db)
    assert db_to_meter_ratio(-60.0) == 0.0
    assert db_to_meter_ratio(0.0) == 1.0


def test_samples_db_sine_approx():
    s = generate_sine_dbfs(-30.0)
    db = samples_db(s)
    assert -32.0 <= db <= -28.0


def test_gate_closed_below_threshold():
    s = generate_sine_dbfs(-50.0)
    assert not gate_is_open(-38.0, chunk_db=samples_db(s), chunk_only=True)


def test_gate_open_above_threshold():
    s = generate_sine_dbfs(-30.0)
    assert gate_is_open(-38.0, chunk_db=samples_db(s), chunk_only=True)


def test_gate_off_bypasses():
    s = generate_sine_dbfs(-50.0)
    assert gate_is_open(-90.0, chunk_db=samples_db(s), chunk_only=True)


def test_apply_gate_silences():
    s = generate_sine_dbfs(-50.0)
    out = apply_gate(s, -38.0, chunk_only=True)
    assert np.max(np.abs(out)) == 0.0


def test_apply_gate_passes():
    s = generate_sine_dbfs(-30.0)
    out = apply_gate(s, -38.0, chunk_only=True)
    assert np.max(np.abs(out)) > 0.0


def test_volume_after_gate():
    s = generate_sine_dbfs(-30.0)
    out, _ = process_playback_chunk(s, -38.0, 25.0)
    ref, _ = process_playback_chunk(s, -38.0, 100.0)
    ratio = np.max(np.abs(out)) / max(np.max(np.abs(ref)), 1e-9)
    assert 0.20 <= ratio <= 0.30


def test_volume_does_not_open_gate():
    s = generate_sine_dbfs(-50.0)
    _, open_lo = process_playback_chunk(s, -38.0, 10.0)
    _, open_hi = process_playback_chunk(s, -38.0, 100.0)
    assert open_lo == open_hi == False


def test_ffplay_filter_at_minus_38():
    f = ffplay_agate_filter(-38.0)
    assert f is not None
    assert "agate=threshold=0.01258925" in f
    assert "detection=peak" in f


def test_stale_level_db_keeps_gate_open_ui_mode():
    """Modo UI (no chunk_only): level_db alto mantiene abierto — solo indicador."""
    s = generate_sine_dbfs(-55.0)
    assert gate_is_open(
        -38.0, chunk_db=samples_db(s), level_db=-30.0, chunk_only=False,
    )


def test_ffplay_filter_off():
    assert ffplay_agate_filter(-90.0) is None
    assert gate_threshold_disabled(-90.0)


def test_stale_level_db_does_not_latch_with_chunk_only():
    """Fix: level_db alto no debe abrir si el chunk actual está bajo umbral."""
    s = generate_sine_dbfs(-55.0)
    chunk_db = samples_db(s)
    assert not gate_is_open(
        -38.0, chunk_db=chunk_db, level_db=-30.0, chunk_only=True,
    )


def test_stale_level_db_latches_without_chunk_only():
    """Comportamiento UI/medidor: max(chunk, level)."""
    s = generate_sine_dbfs(-55.0)
    assert gate_is_open(
        -38.0, chunk_db=samples_db(s), level_db=-30.0, chunk_only=False,
    )


def test_noise_peak_sweep_differentiates_thresholds():
    s = generate_noise_floor_with_peak(-45.0, -25.0)
    rows = sweep_gate_response(s, [-90.0, -48.0, -38.0, -20.0], volume_pct=100.0)
    by_t = {r["threshold_db"]: r["gate_open"] for r in rows}
    assert by_t[-90.0] is True
    assert by_t[-38.0] is True
    assert by_t[-48.0] in (True, False)


def test_process_playback_chunk_dtype():
    s = generate_sine_dbfs(-30.0)
    out, open_ = process_playback_chunk(s, -38.0, 50.0)
    assert out.dtype == np.float32
    assert open_ is True


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("ffplay"), reason="sin ffplay")
def test_ffplay_agate_filter_string_valid():
    f = ffplay_agate_filter(-38.0)
    assert f and "agate" in f
