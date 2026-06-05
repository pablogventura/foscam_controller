"""CLI de diagnóstico de puerta de ruido (python -m foscam.audio_gate_probe)."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

from foscam.audio_gate import (
    ffplay_agate_filter,
    generate_noise_floor_with_peak,
    generate_sine_dbfs,
    process_playback_chunk,
    samples_db,
    sweep_gate_response,
)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 48000) -> None:
    mono = np.asarray(samples, dtype=np.float32).ravel()
    pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def _load_signal(args) -> np.ndarray:
    if args.scenario == "noise_peak":
        return generate_noise_floor_with_peak(
            args.floor_dbf, args.peak_dbf,
            duration_s=args.duration,
        )
    return generate_sine_dbfs(args.input_dbf, duration_s=args.duration)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Probar puerta de ruido con señales sintéticas")
    p.add_argument("--input-dbf", type=float, default=-50.0, help="Nivel del tono (dBFS)")
    p.add_argument("--gate-db", type=float, default=-38.0, help="Umbral de puerta")
    p.add_argument("--volume", type=float, default=100.0, help="Volumen 0-100")
    p.add_argument(
        "--backend", choices=("pyav", "ffplay"), default="pyav",
        help="pyav=puerta software (callback); ffplay=solo cadena agate",
    )
    p.add_argument(
        "--sweep-gate", type=str, default=None,
        help='Umbrales separados por coma, ej: "-90,-48,-38,-20"',
    )
    p.add_argument(
        "--scenario", choices=("sine", "noise_peak"), default="sine",
        help="sine=tono; noise_peak=ruido -45 + pico",
    )
    p.add_argument("--floor-dbf", type=float, default=-45.0)
    p.add_argument("--peak-dbf", type=float, default=-25.0)
    p.add_argument("--duration", type=float, default=0.5)
    p.add_argument("--wav", type=Path, default=None, help="Escribir WAV de salida")
    p.add_argument("--json", action="store_true", help="Salida JSON")
    args = p.parse_args(argv)

    if args.backend == "ffplay" and not args.sweep_gate:
        filt = ffplay_agate_filter(args.gate_db)
        result = {
            "backend": "ffplay",
            "gate_db": args.gate_db,
            "agate_filter": filt,
            "note": "ffplay aplica agate en subproceso; use --backend pyav para simular callback",
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"backend=ffplay  gate_db={args.gate_db:.0f}")
            print(f"agate_filter={filt!r}")
        return 0

    samples = _load_signal(args)
    chunk_db = samples_db(samples)

    if args.sweep_gate:
        thresholds = [float(x.strip()) for x in args.sweep_gate.split(",") if x.strip()]
        rows = sweep_gate_response(samples, thresholds, volume_pct=args.volume)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(
                f"backend={args.backend}  chunk_db={chunk_db:.1f}  "
                f"volume={args.volume:.0f}%  scenario={args.scenario}",
            )
            print("threshold_db  gate_open  output_db")
            for r in rows:
                print(
                    f"{r['threshold_db']:11.0f}  "
                    f"{'ABIERTA' if r['gate_open'] else 'CERRADA':8s}  "
                    f"{r['output_db']:8.1f}",
                )
        return 0

    out, gate_open = process_playback_chunk(samples, args.gate_db, args.volume)
    out_db = samples_db(out) if gate_open else float("-inf")
    result = {
        "backend": args.backend,
        "chunk_db": chunk_db,
        "gate_db": args.gate_db,
        "volume": args.volume,
        "gate_open": gate_open,
        "output_db": out_db,
        "scenario": args.scenario,
    }
    if args.wav:
        _write_wav(args.wav, out)
        result["wav"] = str(args.wav)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        state = "ABIERTA" if gate_open else "CERRADA"
        print(
            f"backend={args.backend}  gate_open={state}  chunk_db={chunk_db:.1f}  "
            f"gate_db={args.gate_db:.0f}  volume={args.volume:.0f}%  "
            f"output_db={out_db}",
        )
        if args.wav:
            print(f"wrote {args.wav}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
