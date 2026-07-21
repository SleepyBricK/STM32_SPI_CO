#!/usr/bin/env python3
"""Bench SPI_STREAM_FW_MAX (free-run, no TIM6): aggregate and per-channel kS/s."""

from __future__ import annotations

import re
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import prep, uv
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
from fw_constants import N_CH


def rms_uv(codes: np.ndarray) -> float:
    return float(np.sqrt(np.mean(uv(codes) ** 2)))


def capture_fw_max(dev, n_per_ch: int, ch: int = 255) -> tuple[list[np.ndarray], float, str]:
    total = n_per_ch * (N_CH if ch == 255 else 1)
    cmd = f"SPI_STREAM_FW_MAX {n_per_ch} {ch} 0"
    reply = run_text_command(dev, cmd, timeout_ms=180000, drain_before=True).strip()
    if not reply.startswith("OK"):
        raise RuntimeError(f"fw max cmd failed: {reply}")

    buckets: list[list[int]] = [[] for _ in range(N_CH)]
    idx = 0
    t0 = time.perf_counter()
    est_s = n_per_ch / 25_000.0 + 5.0
    deadline = t0 + max(120.0, est_s * 2.0)

    while idx < total:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"fw max: got {idx}/{total}")
        p = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60000))
        _, _, _, _, _, sc, _, _, _ = HDR.unpack_from(p, 0)
        for i in range(sc):
            if idx >= total:
                break
            if ch == 255:
                adc = struct.unpack_from("<H", p, 32 + 2 * i)[0]
                c = idx % N_CH
                buckets[c].append(adc)
            else:
                adc = struct.unpack_from("<H", p, 32 + 2 * i)[0]
                buckets[ch].append(adc)
            idx += 1

    wall_s = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=15000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    arrs = [np.array(b[:n_per_ch], dtype=np.uint16) for b in buckets]
    return arrs, wall_s, stats


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="SPI_STREAM_FW_MAX throughput bench")
    ap.add_argument("--n", type=int, default=5000, help="samples per channel (sequences for ch=255)")
    ap.add_argument("--ch", type=int, default=255, help="255=all 16, or 0..15 single")
    ap.add_argument(
        "--allow-unsafe-fw-max",
        action="store_true",
        help="acknowledge that FW_MAX is diagnostic and not production-valid",
    )
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    if not args.allow_unsafe_fw_max:
        ap.error("SPI_STREAM_FW_MAX is diagnostic; pass --allow-unsafe-fw-max to run it")

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    try:
        prep(dev)
        arrs, wall_s, stats = capture_fw_max(dev, args.n, args.ch)

        n_ch = N_CH if args.ch == 255 else 1
        agg_ksps = (args.n * n_ch) / wall_s / 1000.0
        ch_ksps = args.n / wall_s / 1000.0

        clip_m = re.search(r"sample_clip=(\d+)", stats)
        usb_m = re.search(r"usb_ovf=(\d+)", stats)
        clip = int(clip_m.group(1)) if clip_m else -1
        usb_ovf = int(usb_m.group(1)) if usb_m else -1

        print(f"SPI_STREAM_FW_MAX n={args.n} ch={args.ch}")
        print(f"  wall={wall_s:.3f} s")
        print(f"  per-ch kS/s={ch_ksps:.1f}  aggregate kS/s={agg_ksps:.1f}")
        print(f"  sample_clip={clip}  usb_ovf={usb_ovf}")
        if args.ch == 255:
            print(f"  ch0 RMS={rms_uv(arrs[0]):.0f} µV  ch2 RMS={rms_uv(arrs[2]):.0f} µV")
        else:
            print(f"  ch{args.ch} RMS={rms_uv(arrs[args.ch]):.0f} µV")
        print(f"  STATS: {stats}")
    finally:
        try:
            run_text_command(dev, "STOP", drain_before=True)
        except Exception:
            pass
        close_device(dev, ifn)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
