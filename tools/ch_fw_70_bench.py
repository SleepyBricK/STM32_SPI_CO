#!/usr/bin/env python3
"""Bench SPI_STREAM_FW RR8 — production default 40 kS/s/ch only."""

from __future__ import annotations

import re
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import capture_fw16, prep, stats_line, uv
from fw_constants import N_CH, FW_KSPS_DEFAULT
from usb_intan_lib import close_device, open_device, run_text_command


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="SPI_STREAM_FW high-rate RR8 bench")
    ap.add_argument("--ksps", type=int, default=FW_KSPS_DEFAULT, help="kS/s per channel (production: 40)")
    ap.add_argument("--duration", type=float, default=3.0, help="seconds per channel")
    ap.add_argument("--midi", type=int, default=None, help="optional NSS_MIDI before stream")
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    n = max(1000, int(args.duration * args.ksps * 1000))
    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    try:
        prep(dev)
        if args.midi is not None:
            reply = run_text_command(dev, f"NSS_MIDI {args.midi}", drain_before=True).strip()
            print(f"NSS_MIDI {args.midi} -> {reply}")

        print(f"=== SPI_STREAM_FW 255  n={n}/ch  ksps={args.ksps}  (~{args.duration}s) ===")
        t0 = time.perf_counter()
        arrs, stats = capture_fw16(dev, n, args.ksps)
        wall_s = time.perf_counter() - t0

        ch_ksps = min(len(a) for a in arrs) / wall_s / 1000.0
        agg_ksps = ch_ksps * N_CH
        clip = int(m.group(1)) if (m := re.search(r"sample_clip=(\d+)", stats)) else -1
        usb_ovf = int(m.group(1)) if (m := re.search(r"usb_ovf=(\d+)", stats)) else -1
        r2 = float(np.sqrt(np.mean(uv(arrs[2]) ** 2)))

        ok = clip == 0 and usb_ovf == 0 and ch_ksps >= args.ksps * 0.95
        print(f"wall={wall_s:.3f}s  per-ch={ch_ksps:.1f} kS/s  aggregate={agg_ksps:.1f} kS/s")
        print(f"clip={clip}  usb_ovf={usb_ovf}  ch2 RMS={r2:.0f}µV  {'PASS' if ok else 'FAIL'}")
        print(stats_line("ch2", arrs[2], stats))
        print(f"STATS: {stats}")
        return 0 if ok else 1
    finally:
        try:
            run_text_command(dev, "STOP", drain_before=True)
        except Exception:
            pass
        close_device(dev, ifn)


if __name__ == "__main__":
    raise SystemExit(main())
