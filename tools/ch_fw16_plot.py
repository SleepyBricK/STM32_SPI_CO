#!/usr/bin/env python3
"""SPI_STREAM_FW ch=255: all Intan FW channels (0..7) → plot."""

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
from fw_constants import N_CH, FW_KSPS_DEFAULT


def prep(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "INIT_RECORD 350000", drain_before=True)
    run_text_command(dev, "CLEAR_ADC", timeout_ms=30000, drain_before=True)


def capture_fw16(dev, n_per_ch: int, ksps: int) -> tuple[list[np.ndarray], float, str]:
    total = n_per_ch * N_CH
    cmd = f"SPI_STREAM_FW {n_per_ch} 255 0 {ksps}"
    reply = run_text_command(dev, cmd, timeout_ms=120000, drain_before=True).strip()
    if not reply.startswith("OK"):
        raise RuntimeError(f"cmd failed: {reply}")

    buckets: list[list[int]] = [[] for _ in range(N_CH)]
    idx = 0
    t0 = time.perf_counter()
    deadline = t0 + max(180.0, (n_per_ch / max(ksps, 1)) * 1.5 + 60.0)

    while idx < total:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"timeout: got {idx}/{total} samples")
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=30000))
        _, _, flags, _, _, sc, _, _, meta = HDR.unpack_from(pkt, 0)
        tagged = (flags & FLAG_TAG) != 0
        if tagged:
            fc = meta & 0xFF
            cc = (meta >> 8) & 0xFF
            if fc != 0 or cc != N_CH:
                raise RuntimeError(f"meta first={fc} count={cc}, want 0/{N_CH}")
        for i in range(sc):
            if idx >= total:
                break
            if tagged:
                w = struct.unpack_from("<I", pkt, 32 + i * 4)[0]
                ch = (w >> 16) & 0xF
                adc = w & 0xFFFF
            else:
                adc = struct.unpack_from("<H", pkt, 32 + 2 * i)[0]
                ch = idx % N_CH
            if ch < N_CH:
                buckets[ch].append(adc)
            idx += 1

    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    arrs = [np.array(b, dtype=np.uint16) for b in buckets]
    return arrs, elapsed, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot SPI_STREAM_FW all-8 channels")
    ap.add_argument("--n", type=int, default=400, help="samples per channel")
    ap.add_argument("--ksps", type=int, default=FW_KSPS_DEFAULT, help="sequence rate kS/s (production: 40)")
    ap.add_argument(
        "--show-s",
        type=float,
        default=None,
        help="seconds of trace to plot (default: full capture)",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)
    try:
        prep(dev)
        arrs, elapsed, stats = capture_fw16(dev, args.n, args.ksps)
    finally:
        close_device(dev, ifn)

    per_ch_rate_hz = float(args.ksps) * 1000.0
    duration_s = args.n / per_ch_rate_hz if per_ch_rate_hz > 0 else 0.0
    print(
        f"capture: {args.n} samples/ch × 16  (~{duration_s:.2f}s per ch @ {args.ksps} kS/s)  "
        f"usb_read={elapsed:.2f}s"
    )
    print(f"STATS: {stats[:160]}")

    fig, axes = plt.subplots(4, 2, figsize=(16, 14), layout="constrained")
    axes_flat = axes.flat
    show_s = duration_s if args.show_s is None else min(args.show_s, duration_s)
    fs = per_ch_rate_hz

    for ch in range(N_CH):
        codes = arrs[ch]
        uv = (codes.astype(np.float64) - MID) * UV
        n_show = min(len(uv), max(1, int(show_s * fs)))
        t = np.arange(n_show) / fs
        med = int(np.median(codes)) if len(codes) else 0
        rms = float(np.sqrt(np.mean(uv * uv))) if len(codes) else 0.0
        ax = axes_flat[ch]
        ax.plot(t, uv[:n_show], lw=0.4, color=f"C{ch % 10}")
        ax.axhline(0, color="gray", ls="--", lw=0.4)
        ax.set_title(f"ch{ch}  med=0x{med:04X}  RMS={rms:.0f} µV  n={len(codes)}")
        ax.set_xlabel("с")
        ax.set_ylabel("µV")
        ax.grid(True, alpha=0.25)
        print(f"  ch{ch:2d}: med=0x{med:04X}  RMS={rms:6.1f} µV  n={len(codes)}")

    fig.suptitle(
        f"SPI_STREAM_FW 255 — 8×CONVERT @ {args.ksps} kS/s/ch  "
        f"({args.n} samp/ch ≈ {duration_s:.1f} s)",
        fontsize=12,
    )
    out = args.out or Path(__file__).resolve().parent / f"ch_fw8_{args.n}samp_{args.ksps}ksps.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
