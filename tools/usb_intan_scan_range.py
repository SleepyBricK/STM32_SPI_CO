#!/usr/bin/env python3
"""Scan a contiguous RHS2116 channel range using the fast real USB stream."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

from usb_frame_bench import USB_STREAM_FRAME_RESPONSES, parse_frame, validate_frame_header
from usb_intan_lib import EP_IN, FRAME_SIZE, PID, VID, close_device, open_device, run_text_command


def channel_stats(values: list[int]) -> tuple[int, int, float, float, int]:
    signed = [v - 0x8000 for v in values]
    mean = sum(signed) / len(signed)
    std = math.sqrt(sum((x - mean) ** 2 for x in signed) / len(signed))
    out500 = sum(1 for x in signed if abs(x - mean) > 500)
    return min(values), max(values), mean, std, out500


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast Intan channel range scan")
    parser.add_argument("-n", "--samples-per-channel", type=int, default=4096)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--count", type=int, default=16)
    parser.add_argument("--flags", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    args = parser.parse_args()

    if args.samples_per_channel <= 0 or args.first < 0 or args.count <= 0:
        print("samples-per-channel, first and count must describe a non-empty range", file=sys.stderr)
        return 2
    if args.first + args.count > 16:
        print("range must fit channels 0..15", file=sys.stderr)
        return 2

    total_samples = args.samples_per_channel * args.count
    if args.first == 0 and args.count == 16:
        cmd = f"SPI_STREAM_RR16_REAL {total_samples} {args.flags}"
    else:
        cmd = f"SPI_STREAM_RANGE_REAL {total_samples} {args.first} {args.count} {args.flags}"

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)
        print(run_text_command(dev, "ID", timeout_ms=args.timeout_ms, drain_before=True))
        run_text_command(dev, cmd, timeout_ms=args.timeout_ms, drain_before=True)

        buckets: list[list[int]] = [[] for _ in range(args.count)]
        frames_needed = (total_samples + USB_STREAM_FRAME_RESPONSES - 1) // USB_STREAM_FRAME_RESPONSES
        errors = 0
        seq = 0
        idx = 0
        t0 = time.perf_counter()

        for _ in range(frames_needed):
            payload = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=args.timeout_ms))
            errors += validate_frame_header(payload, seq)
            _frame_seq, _first_sc, sample_count, _spi_ovf, _usb_ovf, _ver = parse_frame(payload)
            for i in range(sample_count):
                if idx < total_samples:
                    buckets[idx % args.count].append(struct.unpack_from("<H", payload, 32 + i * 2)[0])
                    idx += 1
            seq += 1

        elapsed = time.perf_counter() - t0
        aggregate_ksps = total_samples / elapsed / 1000.0 if elapsed > 0 else 0.0
        print(
            f"{cmd}: errors={errors} elapsed={elapsed:.6f}s "
            f"aggregate_ksps={aggregate_ksps:.1f} per_ch_ksps={aggregate_ksps / args.count:.1f}"
        )

        for offset, values in enumerate(buckets):
            ch = args.first + offset
            mn, mx, mean, std, out500 = channel_stats(values)
            first = " ".join(f"{v:04X}" for v in values[:8])
            print(
                f"ch{ch:02d}: n={len(values)} min=0x{mn:04X} max=0x{mx:04X} "
                f"signed_mean={mean:.1f} std={std:.1f} out500={out500} first8={first}"
            )

        print(run_text_command(dev, "STATS", timeout_ms=args.timeout_ms, drain_before=True))
        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
