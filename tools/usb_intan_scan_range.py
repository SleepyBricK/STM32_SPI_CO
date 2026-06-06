#!/usr/bin/env python3
"""Scan a contiguous RHS2116 channel range using the fast real USB stream."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

from usb_intan_lib import EP_IN, FRAME_MAGIC, FRAME_SIZE, PID, VID, close_device, open_device, run_text_command

USB_STREAM_FLAG_CHANNEL_TAG = 0x0008
USB_STREAM_FRAME_TAGGED_SAMPLES = 1016
USB_STREAM_FRAME_RESPONSES = 2032
FRAME_HDR = struct.Struct("<IHHIIIIII")


def channel_stats(values: list[int]) -> tuple[int, int, float, float, int]:
    signed = [v - 0x8000 for v in values]
    mean = sum(signed) / len(signed)
    std = math.sqrt(sum((x - mean) ** 2 for x in signed) / len(signed))
    out500 = sum(1 for x in signed if abs(x - mean) > 500)
    return min(values), max(values), mean, std, out500


def parse_frame_header(payload: bytes) -> dict:
    magic, version, flags, frame_seq, first_sc, sample_count, spi_ovf, usb_ovf, meta = (
        FRAME_HDR.unpack_from(payload, 0)
    )
    if magic != FRAME_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08X}")
    if version != 1:
        raise ValueError(f"bad version {version}")

    tagged = bool(flags & USB_STREAM_FLAG_CHANNEL_TAG)
    max_samples = USB_STREAM_FRAME_TAGGED_SAMPLES if tagged else USB_STREAM_FRAME_RESPONSES
    if sample_count > max_samples:
        raise ValueError(f"bad sample_count {sample_count}")

    return {
        "flags": flags,
        "frame_seq": frame_seq,
        "first_sc": first_sc,
        "sample_count": sample_count,
        "spi_ovf": spi_ovf,
        "usb_ovf": usb_ovf,
        "first_ch": meta & 0xFF,
        "ch_count": (meta >> 8) & 0xFF,
        "tagged": tagged,
    }


def validate_frame_header(payload: bytes, expect_seq: int) -> int:
    hdr = parse_frame_header(payload)
    errors = 0
    if hdr["frame_seq"] != expect_seq:
        print(f"ERR frame_seq got={hdr['frame_seq']} want={expect_seq}")
        errors += 1
    if hdr["spi_ovf"] or hdr["usb_ovf"]:
        print(f"ERR overflow spi={hdr['spi_ovf']} usb={hdr['usb_ovf']}")
        errors += 1
    if hdr["sample_count"] == 0:
        print("ERR empty frame")
        errors += 1
    return errors


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
    elif args.first == 0 and args.count == 8:
        cmd = f"SPI_STREAM_RR8_REAL {total_samples} {args.flags}"
    else:
        cmd = f"SPI_STREAM_RANGE_REAL {total_samples} {args.first} {args.count} {args.flags}"

    try:
        dev, ifn = open_device(args.vid, args.pid, reset=args.reset)
        print(run_text_command(dev, "ID", timeout_ms=args.timeout_ms, drain_before=True))
        run_text_command(dev, cmd, timeout_ms=args.timeout_ms, drain_before=True)

        buckets: list[list[int]] = [[] for _ in range(args.count)]
        tag_errors = 0
        errors = 0
        seq = 0
        idx = 0
        tagged = args.count > 1
        max_per_frame = USB_STREAM_FRAME_TAGGED_SAMPLES if tagged else USB_STREAM_FRAME_RESPONSES
        frames_needed = (total_samples + max_per_frame - 1) // max_per_frame
        t0 = time.perf_counter()

        for _ in range(frames_needed):
            payload = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=args.timeout_ms))
            errors += validate_frame_header(payload, seq)
            hdr = parse_frame_header(payload)
            if tagged and not hdr["tagged"]:
                print("ERR expected CHANNEL_TAG in multi-channel stream")
                errors += 1
            if hdr["first_ch"] != args.first or hdr["ch_count"] != args.count:
                print(
                    f"ERR meta first={hdr['first_ch']} count={hdr['ch_count']} "
                    f"want first={args.first} count={args.count}"
                )
                errors += 1

            for i in range(hdr["sample_count"]):
                if idx >= total_samples:
                    break
                if tagged:
                    word = struct.unpack_from("<I", payload, 32 + i * 4)[0]
                    adc = word & 0xFFFF
                    ch = (word >> 16) & 0xF
                    want_ch = args.first + (idx % args.count)
                    if ch != want_ch:
                        tag_errors += 1
                        if tag_errors <= 10:
                            print(
                                f"ERR tag idx={idx} ch={ch} want={want_ch} adc=0x{adc:04X}"
                            )
                else:
                    adc = struct.unpack_from("<H", payload, 32 + i * 2)[0]
                buckets[idx % args.count].append(adc)
                idx += 1
            seq += 1

        if tag_errors:
            errors += tag_errors

        elapsed = time.perf_counter() - t0
        aggregate_ksps = total_samples / elapsed / 1000.0 if elapsed > 0 else 0.0
        print(
            f"{cmd}: errors={errors} tag_errors={tag_errors} elapsed={elapsed:.6f}s "
            f"aggregate_ksps={aggregate_ksps:.1f} per_ch_ksps={aggregate_ksps / args.count:.1f} "
            f"tagged={tagged}"
        )

        for offset, values in enumerate(buckets):
            ch = args.first + offset
            mn, mx, mean, std, out500 = channel_stats(values)
            first = " ".join(f"{v:04X}" for v in values[:8])
            want_n = args.samples_per_channel
            n_ok = "OK" if len(values) == want_n else f"BAD(n={len(values)} want={want_n})"
            print(
                f"ch{ch:02d}: {n_ok} min=0x{mn:04X} max=0x{mx:04X} "
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
