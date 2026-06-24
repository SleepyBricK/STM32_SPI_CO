#!/usr/bin/env python3
"""Sweep SPI_STREAM_FW ksps; report clip and ch0/ch2 RMS."""

from __future__ import annotations

import re
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import capture_fw16, prep, uv
from usb_intan_lib import close_device, open_device, run_text_command

MID = 32768.0


def rms_uv(codes: np.ndarray) -> float:
    return float(np.sqrt(np.mean(uv(codes) ** 2)))


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="samples per channel")
    ap.add_argument(
        "--ksps-list",
        default="40",
        help="comma-separated kS/s (production: 40 only)",
    )
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    ksps_vals = [int(x.strip()) for x in args.ksps_list.split(",") if x.strip()]
    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    print(f"{'ksps':>6}  {'clip':>6}  {'ch0 RMS':>10}  {'ch2 RMS':>10}  {'usb_ovf':>8}  ok")
    print("-" * 52)

    try:
        for ksps in ksps_vals:
            prep(dev)
            try:
                arrs, stats = capture_fw16(dev, args.n, ksps)
            except Exception as exc:
                print(f"{ksps:6d}  FAIL  {exc}")
                continue

            clip = int(re.search(r"sample_clip=(\d+)", stats).group(1)) if re.search(r"sample_clip=", stats) else -1
            usb_ovf = int(re.search(r"usb_ovf=(\d+)", stats).group(1)) if re.search(r"usb_ovf=", stats) else -1
            r0 = rms_uv(arrs[0])
            r2 = rms_uv(arrs[2])
            ok = clip == 0 and r2 < 500.0
            print(f"{ksps:6d}  {clip:6d}  {r0:9.0f}µV  {r2:9.0f}µV  {usb_ovf:8d}  {'PASS' if ok else 'warn'}")
    finally:
        try:
            run_text_command(dev, "STOP", drain_before=True)
        except Exception:
            pass
        close_device(dev, ifn)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
