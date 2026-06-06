#!/usr/bin/env python3
"""Быстрая валидация stream после изменений прошивки."""
from __future__ import annotations

import struct
import sys
import time

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from usb_intan_lib import EP_IN, FRAME_SIZE, open_device, close_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
UV = 0.195
BOUNDARY = 8190


def read_stream(cmd: str, n: int, reset: bool = False) -> tuple[list[int], float, str]:
    dev, ifn = open_device(reset=reset)
    if reset:
        time.sleep(0.3)
    reply = run_text_command(dev, cmd, timeout_ms=120000, drain_before=True).strip()
    if not reply.startswith("OK"):
        close_device(dev, ifn)
        raise RuntimeError(f"cmd failed: {reply}")
    t0 = time.perf_counter()
    codes: list[int] = []
    tagged = 0
    ch_counts = [0] * 8
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=120000))
        _, _, flags, _, _, sc, _, _, _ = HDR.unpack_from(pkt, 0)
        for i in range(sc):
            if len(codes) >= n:
                break
            if flags & 0x0008:
                w = struct.unpack_from("<I", pkt, 32 + i * 4)[0]
                ch = (w >> 16) & 0xF
                codes.append(w & 0xFFFF)
                tagged += 1
                if ch < 8:
                    ch_counts[ch] += 1
            else:
                codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=5000, drain_before=False).strip()
    close_device(dev, ifn)
    extra = ""
    if tagged:
        extra = f" ch={ch_counts}"
    return codes, elapsed, stats + extra


def check_ch4(codes: list[int], elapsed: float, stats: str) -> None:
    uv = np.array([(c - 32768) * UV for c in codes], float)
    spikes = sum(1 for c in codes if 0xC000 <= c < 0xD000)
    ksps = len(codes) / elapsed / 1000.0
    dips = [abs(uv[k * BOUNDARY]) for k in range(1, min(12, len(codes) // BOUNDARY)) if k * BOUNDARY < len(codes)]
    dip_max = max(dips) if dips else 0.0
    print(f"  ch4: n={len(codes)} USB={ksps:.1f} kS/s std={np.std(uv):.1f}uV spikes={spikes} boundary_max={dip_max:.1f}uV")
    print(f"       {stats}")
    if spikes > 0:
        raise SystemExit("FAIL: ch4 spikes 0xC000")
    if dip_max > 80.0:
        raise SystemExit(f"FAIL: ch4 boundary dip {dip_max:.1f} uV")
    if np.std(uv) > 80.0:
        raise SystemExit(f"FAIL: ch4 std {np.std(uv):.1f} uV > 80")
    if ksps < 300.0:
        raise SystemExit(f"FAIL: ch4 USB rate {ksps:.1f} kS/s < 300")


def check_rr8(codes: list[int], elapsed: float, stats: str) -> None:
    ksps = len(codes) / elapsed / 1000.0
    print(f"  RR8: n={len(codes)} USB={ksps:.1f} kS/s aggregate (~{ksps/8:.1f}/ch)")
    print(f"       {stats}")
    if ksps < 200.0:
        raise SystemExit(f"FAIL: RR8 rate {ksps:.1f} kS/s < 200")


def main() -> None:
    print("=== stream_validate_quick ===")
    print("1ch ch4...")
    c, e, s = read_stream("SPI_STREAM_REAL 100000 4 0", 100000, reset=True)
    check_ch4(c, e, s)
    print("RR8...")
    c, e, s = read_stream("SPI_STREAM_RR8_REAL 80000 0", 80000, reset=False)
    check_rr8(c, e, s)
    print("PING...")
    dev, ifn = open_device(reset=False)
    ping = run_text_command(dev, "PING", timeout_ms=5000, drain_before=True).strip()
    close_device(dev, ifn)
    if ping != "PONG":
        raise SystemExit(f"FAIL: PING -> {ping}")
    print("OK all checks passed")


if __name__ == "__main__":
    main()
