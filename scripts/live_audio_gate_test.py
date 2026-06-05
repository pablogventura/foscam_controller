#!/usr/bin/env python3
"""Prueba en vivo: demux RTSP, mide niveles y simula gate en bloques reales."""

from __future__ import annotations

import argparse
import sys
import time

from foscam.audio_gate import (
    GATE_PRESETS,
    gate_is_open,
    process_playback_chunk,
    samples_db,
)


def _probe_rtsp(url: str, seconds: float, gate_thresholds: list[float]) -> dict:
    import av
    import numpy as np

    opts = {"rtsp_flags": "prefer_tcp", "fflags": "nobuffer", "flags": "low_delay"}
    container = av.open(url, options=opts)
    audio_stream = next((s for s in container.streams if s.type == "audio"), None)
    if audio_stream is None:
        container.close()
        raise RuntimeError("sin pista de audio en RTSP")

    chunks_db: list[float] = []
    gate_counts = {t: {"open": 0, "total": 0} for t in gate_thresholds}
    t0 = time.monotonic()

    try:
        for packet in container.demux(audio_stream):
            if time.monotonic() - t0 > seconds:
                break
            for frame in packet.decode():
                if time.monotonic() - t0 > seconds:
                    break
                arr = frame.to_ndarray()
                if arr.dtype != np.float32:
                    arr = arr.astype(np.float32) / 32768.0
                ch = arr.shape[1] if arr.ndim > 1 else 1
                step = 1024 * ch
                for i in range(0, arr.size, step):
                    chunk = arr.ravel()[i:i + step]
                    if chunk.size == 0:
                        continue
                    db = samples_db(chunk)
                    chunks_db.append(db)
                    for thresh in gate_thresholds:
                        gate_counts[thresh]["total"] += 1
                        if gate_is_open(thresh, chunk_db=db, chunk_only=True):
                            gate_counts[thresh]["open"] += 1
    finally:
        container.close()

    if not chunks_db:
        raise RuntimeError("no se recibieron muestras de audio")

    import numpy as np
    arr_db = np.array(chunks_db)
    return {
        "chunks": len(chunks_db),
        "db_min": float(arr_db.min()),
        "db_max": float(arr_db.max()),
        "db_mean": float(arr_db.mean()),
        "db_p50": float(np.percentile(arr_db, 50)),
        "db_p90": float(np.percentile(arr_db, 90)),
        "gate": {
            str(t): {
                "open_pct": 100.0 * v["open"] / max(1, v["total"]),
                "open": v["open"],
                "total": v["total"],
            }
            for t, v in gate_counts.items()
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Test gate con stream RTSP real")
    p.add_argument("--ip", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--port", type=int, default=88)
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument(
        "--gates", default="-90,-48,-38,-20",
        help="Umbrales a evaluar (coma)",
    )
    args = p.parse_args()
    url = f"rtsp://{args.user}:{args.password}@{args.ip}:{args.port}/videoMain"
    thresholds = [float(x.strip()) for x in args.gates.split(",") if x.strip()]

    print(f"RTSP {args.ip}:{args.port}/videoMain  ({args.seconds}s)")
    try:
        stats = _probe_rtsp(url, args.seconds, thresholds)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"chunks={stats['chunks']}  dB min/mean/p90/max: "
          f"{stats['db_min']:.1f} / {stats['db_mean']:.1f} / "
          f"{stats['db_p90']:.1f} / {stats['db_max']:.1f}")
    print("umbral   abierto%   (abiertos/total)")
    for t in thresholds:
        g = stats["gate"][str(t)]
        label = next((k for k, v in GATE_PRESETS.items() if abs(v - t) < 0.5), "")
        extra = f" ({label})" if label else ""
        print(f"{t:6.0f}   {g['open_pct']:6.1f}%    ({g['open']}/{g['total']}){extra}")

    # Señal de sanidad: -38 y -48 deben diferir si hay rango dinámico
    g38 = stats["gate"].get("-38.0") or stats["gate"].get(str(-38.0))
    g48 = stats["gate"].get("-48.0") or stats["gate"].get(str(-48.0))
    if g38 and g48:
        diff = abs(g38["open_pct"] - g48["open_pct"])
        if diff < 1.0 and stats["db_max"] - stats["db_min"] > 3:
            print("AVISO: -38 y -48 casi iguales con señal variable — revisar gate")
        elif diff >= 1.0:
            print(f"OK: diferencia -38 vs -48 = {diff:.1f} pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
