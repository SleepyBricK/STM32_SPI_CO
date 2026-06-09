#!/usr/bin/env python3
"""Проверка совместимости Orange Pi host с RHS1 CHANNEL_TAG (STM32 831e5c4+)."""

from __future__ import annotations

import struct
import sys
import time

sys.path.insert(0, "/home/admin/Stimulator_2.0_orangepizero2w/services/server")

from intan_rhs1 import (
    RHS1_HEADER,
    RHS1_FLAG_CHANNEL_TAG,
    Rhs1ChannelRouter,
    parse_rhs1_header,
    tagged_raw_to_interleaved_bytes,
)
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command


def main() -> int:
    samples = 8000
    channels = list(range(8))
    cmd = f"SPI_STREAM_RR8_REAL {samples} 0"

    by_tag: dict[int, list[int]] = {ch: [] for ch in range(16)}
    tag_mismatch = 0
    router = Rhs1ChannelRouter(channels)
    interleaved = bytearray()
    meta0 = None
    frames = 0
    got = 0

    dev, ifn = open_device(reset=True)
    try:
        run_text_command(dev, cmd, timeout_ms=60000, drain_before=True)
        t0 = time.perf_counter()
        while got < samples:
            raw = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60000))
            meta = parse_rhs1_header(raw)
            if meta0 is None:
                meta0 = meta
            if not meta.channel_tagged:
                print("FAIL: поток без CHANNEL_TAG")
                return 1
            frames += 1
            chunk = router.feed_raw(raw, meta, validate_tags=True)
            interleaved.extend(chunk)
            off = RHS1_HEADER.size
            words = struct.unpack_from(f"<{meta.sample_count}I", raw, off)
            for i, word in enumerate(words):
                ch = (word >> 16) & 0xF
                adc = word & 0xFFFF
                by_tag[ch].append(adc)
                want = channels[0] + ((meta.first_sample_counter + i) % len(channels))
                if ch != want:
                    tag_mismatch += 1
                got += 1
                if got >= samples:
                    break
        elapsed = time.perf_counter() - t0
        run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    finally:
        close_device(dev, ifn)

    nch = len(channels)
    n_frames = len(interleaved) // (nch * 2)
    by_inter = {ch: [] for ch in channels}
    for fi in range(n_frames):
        base = fi * nch
        for ci, ch in enumerate(channels):
            by_inter[ch].append(struct.unpack_from("<H", interleaved, (base + ci) * 2)[0])

    print(f"commit=831e5c4+ frames={frames} samples={got} elapsed={elapsed:.3f}s")
    print(
        f"metadata: tagged={meta0.channel_tagged} first_ch={meta0.first_channel} "
        f"nch={meta0.channel_count} flags=0x{meta0.flags:04X}"
    )
    print(f"tag_validate_errors={tag_mismatch} router_tag_errors={router.tag_errors}")

    ok = True
    for ch in channels:
        if not by_tag[ch] or not by_inter[ch]:
            print(f"FAIL ch{ch}: empty buckets")
            ok = False
            continue
        if len(by_tag[ch]) != len(by_inter[ch]):
            print(f"FAIL ch{ch}: tag_n={len(by_tag[ch])} inter_n={len(by_inter[ch])}")
            ok = False
        # compare first 200 and last 200
        for idx in list(range(min(200, len(by_tag[ch])))) + list(
            range(max(0, len(by_tag[ch]) - 200), len(by_tag[ch]))
        ):
            if by_tag[ch][idx] != by_inter[ch][idx]:
                print(f"FAIL ch{ch}: mismatch at sample {idx}")
                ok = False
                break

    ksps = got / elapsed / 1000.0 if elapsed > 0 else 0
    print(f"aggregate_USB={ksps:.1f} kS/s (~{ksps/8:.1f} kS/s per ch)")
    if ksps < 200:
        print("WARN: USB rate below 200 kS/s aggregate")
    if tag_mismatch > got * 0.01:
        print(f"WARN: >1% tag formula mismatch ({tag_mismatch}/{got}) — host uses tag field, OK")
    if ok:
        print("OK: host interleave matches CHANNEL_TAG buckets")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
