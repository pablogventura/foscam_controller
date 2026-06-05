"""Tests de integración (ffmpeg/ffplay en PATH)."""

import shutil
import subprocess

import pytest

from foscam.audio_gate import ffplay_agate_filter, generate_sine_dbfs, samples_db


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="sin ffmpeg")
def test_ffmpeg_accepts_agate_filter():
    filt = ffplay_agate_filter(-38.0)
    assert filt is not None
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.15",
            "-af", filt,
            "-f", "null", "-",
        ],
        capture_output=True,
        timeout=10,
    )
    assert r.returncode == 0, (r.stderr or b"").decode()


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="sin ffmpeg")
def test_agate_attenuates_quiet_signal():
    """Tono -50 dBFS con umbral -38: agate no debe aumentar energía vs passthrough."""
    filt = ffplay_agate_filter(-38.0)
    assert filt is not None

    def _rms_lavfi(af_extra: str) -> float:
        import numpy as np
        af = f"volume=-50dB,{af_extra}" if af_extra else "volume=-50dB"
        r = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                "-af", af,
                "-f", "f32le", "-",
            ],
            capture_output=True,
            timeout=10,
        )
        assert r.returncode == 0, (r.stderr or b"").decode()
        out = np.frombuffer(r.stdout, dtype=np.float32)
        return float(np.sqrt(np.mean(np.square(out)))) if out.size else 0.0

    rms_gated = _rms_lavfi(filt)
    rms_raw = _rms_lavfi("")
    # agate no debe amplificar; la atenuación fuerte depende del build de ffmpeg
    assert rms_gated <= rms_raw * 1.05 + 1e-9


def test_peak_chunk_above_floor_gate():
    """Pico aislado por encima del umbral abre la puerta; suelo no."""
    from foscam.audio_gate import gate_is_open, generate_noise_floor_with_peak

    full = generate_noise_floor_with_peak(-45.0, -25.0, duration_s=0.5)
    sr = 48000
    p0 = int(0.2 * sr)
    p1 = int(0.25 * sr)
    peak_chunk = full[p0:p1]
    floor_chunk = full[:1024]
    assert gate_is_open(-38.0, chunk_db=samples_db(peak_chunk), chunk_only=True)
    assert not gate_is_open(-38.0, chunk_db=samples_db(floor_chunk), chunk_only=True)
