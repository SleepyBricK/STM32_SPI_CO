#!/usr/bin/env python3
"""
Пошаговый поиск скорости SPI stream с хорошей синусоидой (Moku → Intan).

Сначала медленные настройки (большой SPI_PSCL, большой NSS_MIDI), затем разгон.
Один USB-chunk (≤8188) — без стыка chunk; потом multi-chunk.

  python3 tools/moku_sin_speed_sweep.py --no-reset
  python3 tools/moku_sin_speed_sweep.py --no-reset --ramp-up
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command
from moku_sin_record_test import MokuSine, prep_record, UV_PER_CODE, ADC_MID, FRAME_HDR

SINGLE_CHUNK_MAX = 8188
CORR_GOOD = 0.85


def cmd(dev, text: str) -> str:
    r = run_text_command(dev, text, timeout_ms=120_000, drain_before=True)
    if r.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {r}")
    return r


def set_speed(dev, pscl: int, midi: int) -> None:
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=True)
    cmd(dev, f"SPI_PSCL {pscl}")
    cmd(dev, f"NSS_MIDI {midi}")


def capture(dev, stream_cmd: str, n: int) -> tuple[np.ndarray, float]:
    run_text_command(dev, stream_cmd, timeout_ms=120_000, drain_before=True)
    codes: list[int] = []
    t0 = time.perf_counter()
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60_000))
        _, _, _, _, _, sc, spi_ovf, usb_ovf, _ = FRAME_HDR.unpack_from(pkt, 0)
        if spi_ovf or usb_ovf:
            raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
        for i in range(sc):
            if len(codes) >= n:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    run_text_command(dev, "STOP", timeout_ms=10_000, drain_before=False)
    return np.asarray(codes, dtype=np.uint16), elapsed


def corr_sin(uv: np.ndarray, fs: float, freq: float = 50.0) -> float:
    skip = min(len(uv) // 10, 400)
    x = uv[skip:].astype(np.float64)
    x -= x.mean()
    t = np.arange(len(x)) / fs
    ref = np.sin(2 * np.pi * freq * t)
    ref -= ref.mean()
    xs = x - x.mean()
    cp = float(np.corrcoef(xs, ref)[0, 1])
    cn = float(np.corrcoef(xs, -ref)[0, 1])
    return cp if abs(cp) >= abs(cn) else cn


def eval_point(dev, ch: int, n: int, stream: str, freq: float) -> dict:
    text = f"{stream} {n} {ch} 0"
    codes, elapsed = capture(dev, text, n)
    uv = (codes.astype(np.float64) - ADC_MID) * UV_PER_CODE
    fs = len(codes) / elapsed
    c = corr_sin(uv, fs, freq)
    jumps = int((np.abs(np.diff(uv)) > 800).sum())
    return {
        "n": len(codes),
        "fs": fs,
        "corr": c,
        "abs_corr": abs(c),
        "jumps": jumps,
        "peak_uv": float(np.max(np.abs(uv - uv.mean()))),
    }


def speed_grid_slow_first() -> list[tuple[int, int]]:
    """(pscl, midi) от медленного к быстрому."""
    out: list[tuple[int, int]] = []
    for pscl in (128, 64, 32, 16, 8):
        for midi in (15, 12, 10, 8, 6, 4, 3, 2):
            out.append((pscl, midi))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-moku", action="store_true")
    ap.add_argument("--ch", type=int, default=2)
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--moku-amp-vpp", type=float, default=0.004)
    ap.add_argument("--stream", default="SPI_STREAM_REAL")
    ap.add_argument("--samples", type=int, default=SINGLE_CHUNK_MAX)
    ap.add_argument("--ramp-up", action="store_true", help="после базовой точки — ускорять")
    ap.add_argument("--corr-min", type=float, default=CORR_GOOD)
    args = ap.parse_args()

    moku: MokuSine | None = None
    if not args.no_moku:
        moku = MokuSine("mokugo-002464.local", 1)
        moku.start(freq_hz=args.freq, amp_vpp=args.moku_amp_vpp)
        print("Moku: ON")
        time.sleep(0.1)

    dev, ifn = open_device(reset=not args.no_reset)
    best: dict | None = None
    try:
        prep_record(dev, 350_000)
        print(f"stream={args.stream}  n={args.samples}  corr_min={args.corr_min}")
        print(f"{'pscl':>5} {'midi':>4} {'fs_k':>7} {'|corr|':>7} {'jumps':>6} {'peak':>7}")
        print("-" * 44)

        for pscl, midi in speed_grid_slow_first():
            try:
                set_speed(dev, pscl, midi)
                time.sleep(0.02)
                m = eval_point(dev, args.ch, args.samples, args.stream, args.freq)
            except Exception as e:
                print(f"{pscl:5d} {midi:4d}  ERR {e}")
                continue

            mark = ""
            if m["abs_corr"] >= args.corr_min and m["jumps"] == 0:
                mark = " *"
                if best is None or m["abs_corr"] > best["abs_corr"] or (
                    abs(m["abs_corr"] - best["abs_corr"]) < 0.02 and m["fs"] > best["fs"]
                ):
                    best = {"pscl": pscl, "midi": midi, **m}
                if not args.ramp_up and m["abs_corr"] >= args.corr_min:
                    print("Найдена рабочая точка (single-chunk), останов.")
                    break
            elif m["jumps"] == 0 and (
                best is None or m["abs_corr"] > best.get("abs_corr", 0.0)
            ):
                best = {"pscl": pscl, "midi": midi, **m}

            print(
                f"{pscl:5d} {midi:4d} {m['fs']/1e3:7.1f} {m['abs_corr']:7.3f} "
                f"{m['jumps']:6d} {m['peak_uv']:7.0f}{mark}"
            )

        if best:
            print()
            print(
                f"BEST single-chunk: SPI_PSCL={best['pscl']} NSS_MIDI={best['midi']} "
                f"fs≈{best['fs']/1e3:.1f} kS/s |corr|={best['abs_corr']:.3f}"
            )
            set_speed(dev, best["pscl"], best["midi"])
            if args.ramp_up:
                n_multi = min(175_000, args.samples * 4)
                print(f"Multi-chunk test n={n_multi} ...")
                mm = eval_point(dev, args.ch, n_multi, args.stream, args.freq)
                print(
                    f"  multi: fs={mm['fs']/1e3:.1f}k |corr|={mm['abs_corr']:.3f} "
                    f"jumps={mm['jumps']}"
                )
            print(cmd(dev, "STATS"))
        else:
            print("Порог corr не достигнут. Лучшие точки — смотрите таблицу выше.")
            return 1
    finally:
        close_device(dev, ifn)
        if moku is not None:
            moku.stop()
            print("Moku: Off")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
