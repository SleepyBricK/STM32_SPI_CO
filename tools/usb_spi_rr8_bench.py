#!/usr/bin/env python3
"""8-channel round-robin SPI bench (channels 0..7, RR8)."""

from __future__ import annotations

import argparse
import re
import sys
import time

from usb_intan_lib import EP_IN, FRAME_SIZE, PID, VID, close_device, open_device, run_text_command

RR8_CHANNELS = 8
KV_RE = re.compile(r"(\w+)=([^\s]+)")


def parse_kv(line: str) -> dict[str, str]:
    return {k: v for k, v in KV_RE.findall(line)}


def ksps_from_x10(raw: str | None) -> float | None:
    if raw is None:
        return None
    return int(raw, 10) / 10000.0


def run_rate(dev, cmd: str, timeout_ms: int) -> dict[str, str]:
    reply = run_text_command(dev, cmd, timeout_ms=timeout_ms, drain_before=True)
    print(reply)
    return parse_kv(reply)


def run_stream_counter(dev, samples: int, timeout_ms: int, *, real: bool) -> tuple[int, float]:
    from usb_frame_bench import USB_STREAM_FRAME_RESPONSES, parse_frame, validate_frame, validate_frame_header

    cmd = f"SPI_STREAM_RR8_REAL {samples} 0" if real else f"SPI_STREAM_RR8 {samples} 0"
    run_text_command(dev, cmd, timeout_ms=timeout_ms, drain_before=True)

    frames_needed = (samples + USB_STREAM_FRAME_RESPONSES - 1) // USB_STREAM_FRAME_RESPONSES
    errors = 0
    next_seq = 0
    next_first = 0
    t0 = time.perf_counter()

    for _ in range(frames_needed):
        payload = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
        if real:
            errors += validate_frame_header(payload, next_seq)
        else:
            errors += validate_frame(payload, next_seq, next_first)
        _seq, first_sc, sample_count, _spi, _usb, _v = parse_frame(payload)
        next_seq += 1
        if not real:
            next_first = (first_sc + sample_count) & 0xFFFFFFFF

    elapsed = time.perf_counter() - t0
    return errors, elapsed


def check_phase(samples: int, phase_end: int | None) -> bool:
    want = samples % RR8_CHANNELS
    if phase_end is None:
        print(f"WARN phase_end missing (want {want})")
        return True
    if phase_end != want:
        print(f"ERR phase_end={phase_end} want={want} (samples={samples} n_ch={RR8_CHANNELS})")
        return False
    print(f"OK phase_end={phase_end} (samples mod {RR8_CHANNELS})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="RR8 SPI bench (8 channels round-robin)")
    parser.add_argument("-n", "--samples", type=int, default=50000)
    parser.add_argument("--flags", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--compare-1ch", action="store_true", help="also run SPI_RATE ch0")
    parser.add_argument("--rate", action="store_true", help="SPI_RATE_RR8 only")
    parser.add_argument("--to-ram", action="store_true", help="SPI_TO_RAM_RR8 only")
    parser.add_argument("--stream", action="store_true", help="SPI_STREAM_RR8 counter + frames")
    parser.add_argument(
        "--stream-real",
        action="store_true",
        help="SPI_STREAM_RR8_REAL (header-only frame check)",
    )
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    args = parser.parse_args()

    if args.samples <= 0:
        print("samples must be > 0", file=sys.stderr)
        return 2

    run_all = not (args.rate or args.to_ram or args.stream or args.stream_real)
    errors = 0

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)

        if args.compare_1ch or run_all:
            kv = run_rate(
                dev,
                f"SPI_RATE {args.samples} 0 {args.flags}",
                args.timeout_ms,
            )
            total = ksps_from_x10(kv.get("ksps_x10"))
            if total is not None:
                print(f"  => 1ch total={total:.1f} kS/s\n")

        if args.rate or run_all:
            print("=== SPI_RATE_RR8 ===")
            kv = run_rate(
                dev,
                f"SPI_RATE_RR8 {args.samples} {args.flags}",
                args.timeout_ms,
            )
            total = ksps_from_x10(kv.get("ksps_x10"))
            per_ch = ksps_from_x10(kv.get("ksps_per_ch_x10"))
            wall = ksps_from_x10(kv.get("wall_ksps_x10"))
            wall_ch = ksps_from_x10(kv.get("wall_per_ch_x10"))
            phase = int(kv["phase_end"], 10) if "phase_end" in kv else None
            if not check_phase(args.samples, phase):
                errors += 1
            if total is not None and per_ch is not None:
                print(
                    f"  => total={total:.1f} kS/s  per_ch={per_ch:.1f} kS/s  "
                    f"wall={wall:.1f} wall_ch={wall_ch:.1f}\n"
                )

        if args.to_ram or run_all:
            print("=== SPI_TO_RAM_RR8 ===")
            kv = run_rate(
                dev,
                f"SPI_TO_RAM_RR8 {args.samples} {args.flags}",
                args.timeout_ms,
            )
            total = ksps_from_x10(kv.get("ksps_x10"))
            per_ch = ksps_from_x10(kv.get("ksps_per_ch_x10"))
            phase = int(kv["phase_end"], 10) if "phase_end" in kv else None
            if not check_phase(args.samples, phase):
                errors += 1
            if total is not None and per_ch is not None:
                print(f"  => total={total:.1f} kS/s  per_ch={per_ch:.1f} kS/s\n")

        if args.stream or run_all:
            print("=== SPI_STREAM_RR8 (counter) ===")
            frame_errors, elapsed = run_stream_counter(
                dev, args.samples, args.timeout_ms, real=False
            )
            ksps = args.samples / elapsed / 1000.0 if elapsed > 0 else 0.0
            per_ch = ksps / RR8_CHANNELS
            print(
                f"  => frames errors={frame_errors} elapsed={elapsed:.3f}s "
                f"total={ksps:.1f} kS/s per_ch={per_ch:.1f} kS/s"
            )
            errors += frame_errors

        if args.stream_real:
            print("=== SPI_STREAM_RR8_REAL ===")
            frame_errors, elapsed = run_stream_counter(
                dev, args.samples, args.timeout_ms, real=True
            )
            ksps = args.samples / elapsed / 1000.0 if elapsed > 0 else 0.0
            per_ch = ksps / RR8_CHANNELS
            print(
                f"  => frames errors={frame_errors} elapsed={elapsed:.3f}s "
                f"total={ksps:.1f} kS/s per_ch={per_ch:.1f} kS/s"
            )
            errors += frame_errors

        stats = run_text_command(dev, "STATS", timeout_ms=args.timeout_ms, drain_before=True)
        print(f"\n{stats}")

        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
