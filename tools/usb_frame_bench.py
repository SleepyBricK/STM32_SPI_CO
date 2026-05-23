#!/usr/bin/env python3
"""Validate SYNTH_STREAM RHS1 frames over USB HS bulk IN."""

from __future__ import annotations

import argparse
import struct
import sys
import time

from usb_intan_lib import (
    EP_IN,
    FRAME_MAGIC,
    FRAME_SIZE,
    PID,
    VID,
    close_device,
    open_device,
    run_text_command,
)

USB_STREAM_FRAME_RESPONSES = 2032
FRAME_HDR = struct.Struct("<IHHIIIIII")

assert FRAME_HDR.size == 32, FRAME_HDR.size

IDX_SPOT_CHECK = (0, 1, 239, 240, 241, 496, 1007, 1008, 1264, 2031)


def parse_frame(payload: bytes) -> tuple[int, int, int, int, int, int]:
    if len(payload) != FRAME_SIZE:
        raise ValueError(f"frame length {len(payload)} != {FRAME_SIZE}")

    (
        magic,
        version,
        _flags,
        frame_seq,
        first_sc,
        sample_count,
        spi_ovf,
        usb_ovf,
        _reserved,
    ) = FRAME_HDR.unpack_from(payload, 0)

    if magic != FRAME_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08X}")
    if version != 1:
        raise ValueError(f"bad version {version}")
    if sample_count > USB_STREAM_FRAME_RESPONSES:
        raise ValueError(f"bad sample_count {sample_count}")
    return frame_seq, first_sc, sample_count, spi_ovf, usb_ovf, version


def validate_frame_header(payload: bytes, expect_seq: int) -> int:
    frame_seq, _first_sc, sample_count, spi_ovf, usb_ovf, _ver = parse_frame(payload)
    errors = 0
    if frame_seq != expect_seq:
        print(f"ERR frame_seq got={frame_seq} want={expect_seq}")
        errors += 1
    if spi_ovf != 0 or usb_ovf != 0:
        print(f"ERR overflow spi={spi_ovf} usb={usb_ovf}")
        errors += 1
    if sample_count == 0 or sample_count > USB_STREAM_FRAME_RESPONSES:
        print(f"ERR sample_count={sample_count}")
        errors += 1
    return errors


def validate_frame(payload: bytes, expect_seq: int, expect_first: int) -> int:
    frame_seq, first_sc, sample_count, spi_ovf, usb_ovf, _ver = parse_frame(payload)
    errors = 0

    if frame_seq != expect_seq:
        print(f"ERR frame_seq got={frame_seq} want={expect_seq}")
        errors += 1
    if first_sc != expect_first:
        print(f"ERR first_sc got={first_sc} want={expect_first}")
        errors += 1
    if spi_ovf != 0 or usb_ovf != 0:
        print(f"ERR overflow spi={spi_ovf} usb={usb_ovf}")
        errors += 1

    for i in IDX_SPOT_CHECK:
        if i >= sample_count:
            continue
        off = 32 + i * 2
        got = struct.unpack_from("<H", payload, off)[0]
        want = (first_sc + i) & 0xFFFF
        if got != want:
            print(f"ERR sample i={i} got=0x{got:04X} want=0x{want:04X} off={off}")
            errors += 1

    return errors


def run_once(
    dev,
    samples: int,
    timeout_ms: int,
    *,
    spi_stream: bool = False,
    spi_real: bool = False,
    channel: int = 0,
    flags: int = 0,
) -> tuple[int, float, int]:
    if spi_real:
        cmd = f"SPI_STREAM_REAL {samples} {channel} {flags}"
    elif spi_stream:
        cmd = f"SPI_STREAM {samples} {channel} {flags}"
    else:
        cmd = f"SYNTH_STREAM {samples}"
    run_text_command(dev, cmd, timeout_ms=timeout_ms, drain_before=True)

    frames_needed = (samples + USB_STREAM_FRAME_RESPONSES - 1) // USB_STREAM_FRAME_RESPONSES
    errors = 0
    next_seq = 0
    next_first = 0
    t0 = time.perf_counter()

    for _ in range(frames_needed):
        payload = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
        if spi_real:
            errors += validate_frame_header(payload, next_seq)
        else:
            errors += validate_frame(payload, next_seq, next_first)
        _seq, first_sc, sample_count, _spi, _usb, _v = parse_frame(payload)
        next_seq += 1
        if not spi_real:
            next_first = (first_sc + sample_count) & 0xFFFFFFFF

    elapsed = time.perf_counter() - t0
    return errors, elapsed, samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--samples", type=int, default=50000)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--spi-stream",
        action="store_true",
        help="SPI_STREAM: TIM+DMA SPI, counter payload (step 3)",
    )
    parser.add_argument(
        "--spi-stream-real",
        action="store_true",
        help="SPI_STREAM_REAL: TIM+DMA SPI, real CONVERT RESPONSE (step 4)",
    )
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--flags", type=lambda x: int(x, 0), default=0)
    args = parser.parse_args()

    if args.samples <= 0 or args.runs <= 0:
        print("samples and runs must be > 0", file=sys.stderr)
        return 2

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)
        total_errors = 0

        for run in range(args.runs):
            errors, elapsed, samples = run_once(
                dev,
                args.samples,
                args.timeout_ms,
                spi_stream=args.spi_stream,
                spi_real=args.spi_stream_real,
                channel=args.channel,
                flags=args.flags,
            )
            ksps = samples / elapsed / 1000.0 if elapsed > 0 else 0.0
            mbs = (samples * 2) / elapsed / 1e6 if elapsed > 0 else 0.0
            print(
                f"run={run + 1}/{args.runs} samples={samples} errors={errors} "
                f"elapsed={elapsed:.6f}s ksps={ksps:.1f} payload_MBps={mbs:.3f}"
            )
            total_errors += errors

        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
