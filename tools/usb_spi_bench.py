#!/usr/bin/env python3
"""Measure Intan SPI CONVERT throughput via USB (no bulk sample transfer)."""

import argparse
import re
import sys

from usb_intan_lib import PID, VID, close_device, open_device, run_text_command

KSPS_RE = re.compile(
    r"ksps_total=(?P<total>\d+\.\d+|\d+)(?:\s+ksps_per_ch=(?P<perch>\d+\.\d+|\d+))?"
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SPI-only ksps benchmark over USB (BENCH / BENCH_FAST / BENCH_DMA / BENCH_TIMCS)",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="BENCH_DMA",
        choices=("BENCH", "BENCH_FAST", "BENCH_DMA", "BENCH_TIMCS", "BENCH_TIM"),
        help="bench path (default: BENCH_DMA = max SPI throughput)",
    )
    parser.add_argument("-n", "--samples", type=int, default=50000, help="number of CONVERT commands")
    parser.add_argument("--channel", type=int, default=63, help="0-15 or 63 auto (default 63)")
    parser.add_argument(
        "--target-ksps",
        type=int,
        default=600,
        help="for BENCH_TIMCS only (100-720, default 600)",
    )
    parser.add_argument("--init-record", type=int, default=0, help="call INIT_RECORD ksps before bench (0=skip)")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="USB bus reset before test (default: on)",
    )
    args = parser.parse_args()

    if args.samples <= 0 or args.samples > 2_000_000:
        print("samples must be 1..2000000", file=sys.stderr)
        return 2
    if not (0 <= args.channel <= 63):
        print("channel must be 0..63", file=sys.stderr)
        return 2

    mode = args.mode
    if mode == "BENCH_TIM":
        mode = "BENCH_TIMCS"

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)

        if args.init_record > 0:
            ksps = args.init_record
            reply = run_text_command(dev, f"INIT_RECORD {ksps}", timeout_ms=args.timeout_ms)
            print(reply)

        if mode == "BENCH_TIMCS":
            cmd = f"{mode} {args.samples} {args.channel} {args.target_ksps}"
        else:
            cmd = f"{mode} {args.samples} {args.channel}"

        reply = run_text_command(dev, cmd, timeout_ms=args.timeout_ms)
        print(reply)

        m = KSPS_RE.search(reply)
        if m:
            total = m.group("total")
            perch = m.group("perch")
            print(f"spi_ksps_total={total}")
            if perch is not None:
                print(f"spi_ksps_per_ch={perch}")

        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
