#!/usr/bin/env python3
"""Захват SPI_STREAM_RANGE_REAL / RR8 с CHANNEL_TAG и график по каналам."""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
UV = 0.195
MID = 32768.0
FLAG_TAG = 0x0008
TAGGED_MAX = 1016


def stream_cmd(first: int, count: int, total: int) -> str:
    if first == 0 and count == 16:
        return f"SPI_STREAM_RR16_REAL {total} 0"
    if first == 0 and count == 8:
        return f"SPI_STREAM_RR8_REAL {total} 0"
    return f"SPI_STREAM_RANGE_REAL {total} {first} {count} 0"


def read_tagged(first: int, count: int, total: int, reset: bool) -> tuple[list[np.ndarray], float, str]:
    dev, ifn = open_device(reset=reset)
    if reset:
        time.sleep(0.3)
    cmd = stream_cmd(first, count, total)
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True).strip()
    if not reply.startswith("OK"):
        close_device(dev, ifn)
        raise RuntimeError(f"cmd failed: {reply}")

    buckets: list[list[int]] = [[] for _ in range(count)]
    idx = 0
    t0 = time.perf_counter()
    while idx < total:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=600000))
        _, _, flags, _, _, sc, _, _, meta = HDR.unpack_from(pkt, 0)
        if not (flags & FLAG_TAG):
            close_device(dev, ifn)
            raise RuntimeError("expected CHANNEL_TAG (flags bit3); host must read uint32 tagged words")
        fc, cc = meta & 0xFF, (meta >> 8) & 0xFF
        if fc != first or cc != count:
            raise RuntimeError(f"meta first={fc} count={cc} want {first}/{count}")
        for i in range(sc):
            if idx >= total:
                break
            w = struct.unpack_from("<I", pkt, 32 + i * 4)[0]
            ch = (w >> 16) & 0xF
            adc = w & 0xFFFF
            want = first + (idx % count)
            if ch != want:
                print(f"WARN tag idx={idx} ch={ch} want={want}", file=sys.stderr)
            if first <= ch < first + count:
                buckets[ch - first].append(adc)
            idx += 1
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    close_device(dev, ifn)
    arrs = [np.array(b, dtype=np.uint16) for b in buckets]
    return arrs, elapsed, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Range/RR8 plot with CHANNEL_TAG decode")
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--rate", type=float, default=300000.0, help="aggregate kS/s для расчёта N")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    total = int(args.duration * args.rate)
    arrs, elapsed, stats = read_tagged(args.first, args.count, total, args.reset)
    agg = sum(len(a) for a in arrs) / elapsed / 1000.0

    rows = (args.count + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(14, 3 * rows), layout="constrained")
    axes_flat = np.atleast_1d(axes).flat
    show_s = min(3.0, args.duration)

    for i, codes in enumerate(arrs):
        ch = args.first + i
        uv = (codes.astype(np.float64) - MID) * UV
        fs = len(codes) / elapsed if elapsed > 0 else 1.0
        n = min(len(uv), int(show_s * fs))
        t = np.arange(n) / fs
        rms = float(np.sqrt(np.mean(uv * uv)))
        ax = axes_flat[i]
        ax.plot(t, uv[:n], lw=0.35)
        ax.set_title(f"ch{ch}  RMS={rms:.0f} µV  n={len(codes)}")
        ax.set_xlabel("с")
        ax.set_ylabel("µV")
        ax.grid(True, alpha=0.3)
        print(f"ch{ch}: n={len(codes)} rms={rms:.1f} uV per_ch={len(codes)/elapsed/1000:.1f} kS/s")

    for j in range(args.count, len(axes_flat)):
        axes_flat[j].set_visible(False)

    fig.suptitle(
        f"{stream_cmd(args.first, args.count, total)}  agg={agg:.0f} kS/s  {stats[:80]}...",
        fontsize=11,
    )
    out = args.out or Path(__file__).resolve().parent / f"range_{args.first}_{args.count}_plot.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"saved {out}")
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
