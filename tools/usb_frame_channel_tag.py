#!/usr/bin/env python3
"""Validate RHS1 CHANNEL_TAG payload (uint32: ADC + 4-bit channel)."""

from __future__ import annotations

import argparse
import struct
import sys
import time

from usb_intan_lib import EP_IN, FRAME_MAGIC, FRAME_SIZE, close_device, open_device, run_text_command

USB_STREAM_FLAG_CHANNEL_TAG = 0x0008
USB_STREAM_FLAG_REAL_ADC = 0x0002
USB_STREAM_FRAME_TAGGED_SAMPLES = 1016
FRAME_HDR = struct.Struct("<IHHIIIIII")


def parse_header(payload: bytes) -> dict:
    if len(payload) != FRAME_SIZE:
        raise ValueError(f"frame length {len(payload)} != {FRAME_SIZE}")

    magic, version, flags, frame_seq, first_sc, sample_count, spi_ovf, usb_ovf, meta = (
        FRAME_HDR.unpack_from(payload, 0)
    )
    if magic != FRAME_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08X}")
    if version != 1:
        raise ValueError(f"bad version {version}")

    first_ch = meta & 0xFF
    ch_count = (meta >> 8) & 0xFF
    ch_bits = (meta >> 24) & 0x7
    tagged = bool(flags & USB_STREAM_FLAG_CHANNEL_TAG)
    max_samples = USB_STREAM_FRAME_TAGGED_SAMPLES if tagged else 2032
    if sample_count > max_samples:
        raise ValueError(f"bad sample_count {sample_count} max={max_samples}")

    return {
        "flags": flags,
        "frame_seq": frame_seq,
        "first_sc": first_sc,
        "sample_count": sample_count,
        "spi_ovf": spi_ovf,
        "usb_ovf": usb_ovf,
        "first_ch": first_ch,
        "ch_count": ch_count,
        "ch_bits": ch_bits,
        "tagged": tagged,
    }


def expected_channel(global_idx: int, first_ch: int, ch_count: int) -> int:
    return first_ch + (global_idx % ch_count)


def validate_tagged_frame(payload: bytes, hdr: dict, expect_seq: int, expect_first_sc: int) -> int:
    errors = 0
    if hdr["frame_seq"] != expect_seq:
        print(f"ERR frame_seq got={hdr['frame_seq']} want={expect_seq}")
        errors += 1
    if hdr["first_sc"] != expect_first_sc:
        print(f"ERR first_sc got={hdr['first_sc']} want={expect_first_sc}")
        errors += 1
    if not hdr["tagged"]:
        print("ERR expected CHANNEL_TAG flag")
        errors += 1
    if hdr["spi_ovf"] or hdr["usb_ovf"]:
        print(f"ERR overflow spi={hdr['spi_ovf']} usb={hdr['usb_ovf']}")
        errors += 1
    if hdr["ch_count"] <= 1:
        print(f"ERR ch_count={hdr['ch_count']} expected >1 for tagged stream")
        errors += 1

    for i in range(hdr["sample_count"]):
        off = 32 + i * 4
        word = struct.unpack_from("<I", payload, off)[0]
        adc = word & 0xFFFF
        ch = (word >> 16) & 0xF
        want_ch = expected_channel(hdr["first_sc"] + i, hdr["first_ch"], hdr["ch_count"])
        if ch != want_ch:
            print(
                f"ERR sample[{i}] global={hdr['first_sc'] + i} "
                f"ch={ch} want={want_ch} adc=0x{adc:04X} word=0x{word:08X}"
            )
            errors += 1
            if errors >= 20:
                print("ERR too many channel mismatches, stopping frame")
                break

    return errors


def validate_untagged_frame(payload: bytes, hdr: dict, expect_seq: int) -> int:
    errors = 0
    if hdr["frame_seq"] != expect_seq:
        print(f"ERR frame_seq got={hdr['frame_seq']} want={expect_seq}")
        errors += 1
    if hdr["tagged"]:
        print("ERR unexpected CHANNEL_TAG flag on single-channel stream")
        errors += 1
    if hdr["spi_ovf"] or hdr["usb_ovf"]:
        print(f"ERR overflow spi={hdr['spi_ovf']} usb={hdr['usb_ovf']}")
        errors += 1
    if hdr["sample_count"] == 0:
        print("ERR empty frame")
        errors += 1
    return errors


def run_test(
    dev,
    cmd: str,
    samples: int,
    timeout_ms: int,
    *,
    tagged: bool,
    first_ch: int,
    ch_count: int,
) -> tuple[int, float]:
    run_text_command(dev, cmd, timeout_ms=timeout_ms, drain_before=True)

    max_per_frame = USB_STREAM_FRAME_TAGGED_SAMPLES if tagged else 2032
    frames_needed = (samples + max_per_frame - 1) // max_per_frame
    errors = 0
    next_seq = 0
    next_first = 0
    t0 = time.perf_counter()

    for _ in range(frames_needed):
        payload = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
        hdr = parse_header(payload)
        if tagged:
            errors += validate_tagged_frame(payload, hdr, next_seq, next_first)
            next_first = (hdr["first_sc"] + hdr["sample_count"]) & 0xFFFFFFFF
        else:
            errors += validate_untagged_frame(payload, hdr, next_seq)
        next_seq += 1

    elapsed = time.perf_counter() - t0
    return errors, elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=8128)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("rr8", "range", "single"),
        default="rr8",
        help="rr8=SPI_STREAM_RR8_REAL, range=SPI_STREAM_RANGE_REAL 2..5, single=SPI_STREAM_REAL ch0",
    )
    args = parser.parse_args()

    if args.samples <= 0:
        print("samples must be > 0", file=sys.stderr)
        return 2

    if args.mode == "rr8":
        cmd = f"SPI_STREAM_RR8_REAL {args.samples} 0"
        tagged = True
        first_ch, ch_count = 0, 8
    elif args.mode == "range":
        cmd = f"SPI_STREAM_RANGE_REAL {args.samples} 2 4 0"
        tagged = True
        first_ch, ch_count = 2, 4
    else:
        cmd = f"SPI_STREAM_REAL {args.samples} 0 0"
        tagged = False
        first_ch, ch_count = 0, 1

    try:
        dev, ifn = open_device(reset=not args.no_reset)
        errors, elapsed = run_test(
            dev,
            cmd,
            args.samples,
            args.timeout_ms,
            tagged=tagged,
            first_ch=first_ch,
            ch_count=ch_count,
        )
        ksps = args.samples / elapsed / 1000.0 if elapsed > 0 else 0.0
        print(
            f"mode={args.mode} samples={args.samples} errors={errors} "
            f"elapsed={elapsed:.3f}s ksps={ksps:.1f} tagged={tagged}"
        )
        close_device(dev, ifn)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
