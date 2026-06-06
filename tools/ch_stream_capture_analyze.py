#!/usr/bin/env python3
"""Захват 1ch SPI_STREAM_REAL и анализ как в intan_rhs2116_ch*_analysis.md."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from usb_intan_lib import EP_IN, FRAME_SIZE, open_device, close_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
UV = 0.195
BOUNDARY = 8190
SKIP_START = 5000


def read_stream(channel: int, n: int, reset: bool = True) -> tuple[np.ndarray, float, str]:
    dev, ifn = open_device(reset=reset)
    if reset:
        time.sleep(0.3)
    cmd = f"SPI_STREAM_REAL {n} {channel} 0"
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True).strip()
    if not reply.startswith("OK"):
        close_device(dev, ifn)
        raise RuntimeError(f"cmd failed: {reply}")
    t0 = time.perf_counter()
    codes: list[int] = []
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=600000))
        _, _, _, _, _, sc, _, _, _ = HDR.unpack_from(pkt, 0)
        for i in range(sc):
            if len(codes) >= n:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    close_device(dev, ifn)
    return np.array(codes, dtype=np.uint16), elapsed, stats


def mad_sigma(uv: np.ndarray) -> float:
    med = float(np.median(uv))
    mad = float(np.median(np.abs(uv - med)))
    return mad * 1.4826


def boundary_dips(uv: np.ndarray, offsets: tuple[int, ...]) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    n_bound = len(uv) // BOUNDARY
    for off in offsets:
        dips: list[float] = []
        for k in range(1, min(n_bound, 500)):
            idx = k * BOUNDARY + off
            if idx >= len(uv):
                break
            dips.append(float(abs(uv[idx])))
        out[off] = dips
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, default=2)
    parser.add_argument("-n", "--samples", type=int, default=3_500_000)
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--csv-ref-rms", type=float, default=22.03, help="RMS из md для сравнения")
    args = parser.parse_args()

    print(f"=== ch{args.channel} capture analyze n={args.samples} ===")
    codes, elapsed, stats = read_stream(args.channel, args.samples, reset=args.reset)
    uv = (codes.astype(np.float64) - 32768.0) * UV

    ksps = len(codes) / elapsed / 1000.0
    rms_all = float(np.sqrt(np.mean(uv * uv)))
    rms_skip = float(np.sqrt(np.mean(uv[SKIP_START:] * uv[SKIP_START:]))) if len(uv) > SKIP_START else rms_all
    mad = mad_sigma(uv[SKIP_START:] if len(uv) > SKIP_START else uv)

    print(f"USB wall: {ksps:.1f} kS/s elapsed={elapsed:.2f}s")
    print(f"STATS: {stats}")
    print()
    print("| metric | capture | md ch2 ref |")
    print("|--------|--------:|-----------:|")
    print(f"| RMS all | {rms_all:.2f} uV | {args.csv_ref_rms:.2f} uV |")
    print(f"| RMS skip {SKIP_START} | {rms_skip:.2f} uV | 20.35 uV |")
    print(f"| MAD robust | {mad:.2f} uV | 13.88 uV |")
    print(f"| mean | {np.mean(uv):.2f} uV | 1.58 uV |")
    print(f"| min | {np.min(uv):.2f} uV | -578.76 uV |")
    print(f"| max | {np.max(uv):.2f} uV | +809 uV |")
    spikes_c = int(np.sum((codes >= 0xC000) & (codes < 0xD000)))
    print(f"| spikes 0xC000 | {spikes_c} | (ch2 md: 0 >1mV) |")

    for thr in (100, 500, 1000):
        c = int(np.sum(np.abs(uv) > thr))
        print(f"| abs>{thr} uV | {c} | md ch2 |")

    print()
    print("Startup (first 60 samples, uV):")
    for i in range(min(60, len(uv))):
        if i in (27, 32, 40, 49) or i < 10:
            print(f"  sample {i:4d}: {uv[i]:7.1f} uV  raw=0x{codes[i]:04X}")

    offsets = (0, 20, 21, 1840, 1841)
    dips = boundary_dips(uv, offsets)
    print()
    print(f"Boundary dips |uV| at k*{BOUNDARY}+offset (max over k=1..499):")
    for off in offsets:
        d = dips[off]
        mx = max(d) if d else 0.0
        med = float(np.median(d)) if d else 0.0
        print(f"  offset +{off:4d}: max={mx:7.1f} uV  median={med:7.1f} uV  (md ch2 early +20/21 ~500-580 uV)")

    # classic boundary index k*8190
    classic = [abs(float(uv[k * BOUNDARY])) for k in range(1, min(len(uv) // BOUNDARY, 20))]
    if classic:
        print(f"  k*8190 (offset 0): max={max(classic):.1f} uV median={float(np.median(classic)):.1f} uV")

    ok_rms = rms_skip < 80.0
    ok_bnd = max(classic) < 80.0 if classic else True
    ok_sp = spikes_c == 0
    print()
    if ok_rms and ok_bnd and ok_sp:
        print("VERDICT: прошивка сейчас сильно лучше md CSV (нет 8190-провалов ~500 uV при норм RMS)")
    elif ok_bnd and ok_sp:
        print("VERDICT: boundary/spikes OK; RMS выше эталона 2.7 uV — проверить аналог/условия")
    else:
        print("VERDICT: остаются структурные артефакты — см. boundary/spikes")
    return 0 if (ok_rms and ok_bnd and ok_sp) else 1


if __name__ == "__main__":
    raise SystemExit(main())
