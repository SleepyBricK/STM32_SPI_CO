#!/usr/bin/env python3
"""Measure STM32H743 Intan USB bulk sample streaming throughput."""

import argparse
import struct
import sys
import time

from usb_intan_lib import PID, VID, close_device, open_device, read_exact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--samples", type=int, default=50000)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--flags", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--dump-first", type=int, default=8)
    parser.add_argument(
        "--rr8",
        action="store_true",
        help="8-channel round-robin (STREAM8: ch 0..7 interleaved, ~total/8 ksps per ch)",
    )
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="USB bus reset before STREAM (default: on)",
    )
    args = parser.parse_args()

    if args.samples <= 0:
        print("samples must be > 0", file=sys.stderr)
        return 2

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)
        if args.rr8:
            cmd = f"STREAM8 {args.samples} {args.flags}\n".encode("ascii")
        else:
            cmd = f"STREAM {args.samples} {args.channel} {args.flags}\n".encode("ascii")
        dev.write(0x01, cmd, timeout=args.timeout_ms)
        t0 = time.perf_counter()
        payload = read_exact(dev, args.samples * 2, args.timeout_ms)
        elapsed = time.perf_counter() - t0

        first_count = min(args.dump_first, args.samples)
        first = struct.unpack_from("<" + "H" * first_count, payload, 0) if first_count else ()
        ksps_total = args.samples / elapsed / 1000.0
        print(f"samples={args.samples} bytes={len(payload)} elapsed={elapsed:.6f}s")
        print(f"ksps_total={ksps_total:.3f}")
        if args.rr8:
            print(f"ksps_per_ch={ksps_total / 8.0:.3f}")
        print(f"throughput={len(payload) / elapsed / 1e6:.3f} MB/s")
        print("first=" + " ".join(f"0x{x:04X}" for x in first))
        try:
            print(f"pyusb speed={dev.speed}")
        except Exception:
            pass
        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        msg = str(exc).lower()
        if "not found" in msg:
            print(
                "hint: после прошивки ST-Link переподключите кабель USB3300→Mac "
                "(не ST-Link). Проверка: system_profiler SPUSBDataType | grep 5741",
                file=sys.stderr,
            )
        else:
            print(
                "hint: прервённый STREAM — переподключите USB3300 или запустите с --reset",
                file=sys.stderr,
            )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
