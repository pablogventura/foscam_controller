"""Puerta de ruido de audio: funciones puras testeables (sin Tk ni hilos)."""

from __future__ import annotations

import math
from typing import Optional, Union

import numpy as np

AUDIO_DB_FLOOR = -60.0
AUDIO_DB_CEIL = 0.0
METER_DB_MIN = AUDIO_DB_FLOOR
METER_DB_MAX = AUDIO_DB_CEIL
GATE_SLIDER_MIN = -90
GATE_SLIDER_MAX = -20
GATE_PRESETS = {"Llanto": -38.0, "Suave": -48.0, "Off": -90.0}


def db_to_meter_ratio(
    db: float,
    db_min: float = METER_DB_MIN,
    db_max: float = METER_DB_MAX,
) -> float:
    """Posición 0..1 en el medidor (misma escala para barra y línea de umbral)."""
    clamped = max(db_min, min(db_max, float(db)))
    span = db_max - db_min
    if span <= 0:
        return 0.0
    return (clamped - db_min) / span


def samples_db(samples: Union[np.ndarray, list, None]) -> float:
    """Nivel RMS en dBFS de un bloque de muestras."""
    if samples is None:
        return AUDIO_DB_FLOOR
    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return AUDIO_DB_FLOOR
    rms = float(np.sqrt(np.mean(np.square(arr))))
    return max(AUDIO_DB_FLOOR, min(AUDIO_DB_CEIL, 20.0 * math.log10(rms + 1e-9)))


def gate_threshold_disabled(threshold_db: float, min_db: float = GATE_SLIDER_MIN) -> bool:
    return float(threshold_db) <= float(min_db) + 1


def gate_is_open(
    threshold_db: float,
    *,
    chunk_db: Optional[float] = None,
    level_db: Optional[float] = None,
    min_db: float = GATE_SLIDER_MIN,
    chunk_only: bool = False,
) -> bool:
    """
    True si el audio debe pasar la puerta.

    chunk_only=True: solo el bloque actual (callback de reproducción, pre-volumen).
    chunk_only=False: max(chunk, level) para indicador UI / medidor.
    """
    if gate_threshold_disabled(threshold_db, min_db):
        return True
    thresh = float(threshold_db)
    if chunk_only:
        if chunk_db is None:
            return False
        return float(chunk_db) >= thresh
    if chunk_db is not None and level_db is not None:
        return max(float(chunk_db), float(level_db)) >= thresh
    if level_db is not None:
        return float(level_db) >= thresh
    if chunk_db is not None:
        return float(chunk_db) >= thresh
    return False


def apply_gate(
    samples: np.ndarray,
    threshold_db: float,
    *,
    level_db: Optional[float] = None,
    chunk_only: bool = True,
) -> np.ndarray:
    """Silencia muestras si la puerta está cerrada."""
    arr = np.asarray(samples, dtype=np.float32)
    chunk_db = samples_db(arr)
    if gate_is_open(
        threshold_db,
        chunk_db=chunk_db,
        level_db=level_db,
        chunk_only=chunk_only,
    ):
        return arr
    return np.zeros_like(arr)


def apply_playback_volume(samples: np.ndarray, volume_pct: float) -> np.ndarray:
    vol = max(0.0, min(100.0, float(volume_pct))) / 100.0
    arr = np.asarray(samples, dtype=np.float32)
    return arr * vol


def process_playback_chunk(
    samples: np.ndarray,
    threshold_db: float,
    volume_pct: float,
    *,
    level_db: Optional[float] = None,
) -> tuple[np.ndarray, bool]:
    """Puerta pre-volumen + gain. Devuelve (salida, gate_abierta)."""
    gated = apply_gate(samples, threshold_db, level_db=level_db, chunk_only=True)
    open_ = gate_is_open(
        threshold_db,
        chunk_db=samples_db(samples),
        level_db=level_db,
        chunk_only=True,
    )
    return apply_playback_volume(gated, volume_pct), open_


def ffplay_agate_filter(threshold_db: float, min_db: float = GATE_SLIDER_MIN) -> Optional[str]:
    """Cadena -af para ffplay; None si umbral desactivado."""
    db = float(threshold_db)
    if gate_threshold_disabled(db, min_db):
        return None
    th_lin = max(1e-6, min(1.0, 10 ** (db / 20.0)))
    return (
        f"agate=threshold={th_lin:.8f}:ratio=9000:range=1:"
        f"attack=0.01:release=0.05:detection=peak"
    )


def generate_sine_dbfs(
    dbfs: float,
    *,
    sample_rate: int = 48000,
    duration_s: float = 0.1,
    channels: int = 1,
) -> np.ndarray:
    """Tono sinusoidal con RMS ≈ dbfs dBFS."""
    rms_amp = 10 ** (float(dbfs) / 20.0)
    amp = rms_amp * math.sqrt(2.0)
    n = max(1, int(sample_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / sample_rate
    wave = (amp * np.sin(2.0 * math.pi * 440.0 * t)).astype(np.float32)
    if channels > 1:
        wave = np.column_stack([wave] * channels).ravel()
    return wave


def generate_noise_floor_with_peak(
    floor_dbfs: float = -45.0,
    peak_dbfs: float = -25.0,
    *,
    sample_rate: int = 48000,
    duration_s: float = 0.5,
    peak_start_s: float = 0.2,
    peak_duration_s: float = 0.05,
    channels: int = 1,
    seed: int = 42,
) -> np.ndarray:
    """Ruido ambiente constante + pico breve (escenario realista)."""
    rng = np.random.default_rng(seed)
    n = max(1, int(sample_rate * duration_s))
    floor_amp = 10 ** (float(floor_dbfs) / 20.0)
    peak_amp = 10 ** (float(peak_dbfs) / 20.0)
    noise = rng.standard_normal(n).astype(np.float32) * floor_amp
    p0 = int(peak_start_s * sample_rate)
    p1 = min(n, p0 + max(1, int(peak_duration_s * sample_rate)))
    t = np.arange(p1 - p0, dtype=np.float64) / sample_rate
    noise[p0:p1] += (peak_amp * np.sin(2.0 * math.pi * 880.0 * t)).astype(np.float32)
    if channels > 1:
        noise = np.column_stack([noise] * channels).ravel()
    return noise


def sweep_gate_response(
    samples: np.ndarray,
    thresholds: list[float],
    volume_pct: float = 100.0,
) -> list[dict]:
    """Evalúa paso de gate por umbral (para probe CLI)."""
    chunk_db = samples_db(samples)
    rows = []
    for thresh in thresholds:
        out, open_ = process_playback_chunk(samples, thresh, volume_pct)
        out_db = samples_db(out) if open_ else AUDIO_DB_FLOOR
        rows.append({
            "threshold_db": float(thresh),
            "gate_open": open_,
            "chunk_db": chunk_db,
            "output_db": out_db,
            "output_rms": float(np.sqrt(np.mean(np.square(out)))) if out.size else 0.0,
        })
    return rows
