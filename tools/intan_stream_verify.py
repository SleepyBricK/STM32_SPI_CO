#!/usr/bin/env python3
"""
Проверка SPI stream vs CONVERT (Intan RHS2116, ch на GND).

Запуск после прошивки (плата подключена):
  python3 tools/intan_stream_verify.py --no-reset
  python3 tools/intan_stream_verify.py --ch 2 --lengths 256,1024,4096,8188

Критерии PASS (GND):
  CONVERT std < 50 µV
  stream good > 95%, std < 100 µV, med ~0x7FFx
  sample_clip=0 в STATS после stream
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command  # noqa: E402

HDR = struct.Struct("<IHHIIIIII")
UV_PER_CODE = 0.195
ADC_MID = 32768.0


def prep(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "INIT_RECORD 350000", drain_before=True)
    run_text_command(dev, "CLEAR_ADC", timeout_ms=30000, drain_before=True)


def convert_stats(dev, ch: int, n: int = 50) -> tuple[float, float, int]:
    codes: list[int] = []
    for _ in range(n):
        r = run_text_command(dev, f"CONVERT {ch} 0", drain_before=True)
        m = re.search(r"value=0x([0-9A-Fa-f]+)", r)
        if m:
            codes.append(int(m.group(1), 16))
    x = np.array(codes, dtype=np.float64)
    med = float(np.median(x))
    std_uv = float(np.std((x - ADC_MID) * UV_PER_CODE))
    return med, std_uv, len(codes)


def stream_capture(dev, cmd: str, ch: int, n: int) -> np.ndarray:
    if cmd == "SPI_STREAM_FW":
        line = f"{cmd} {n} {ch} 0 20"
    else:
        line = f"{cmd} {n} {ch} 0"
    run_text_command(dev, line, timeout_ms=180000, drain_before=True)
    codes: list[int] = []
    t0 = time.perf_counter()
    while len(codes) < n:
        p = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=120000))
        _, _, _, _, _, sc, _, _, _ = HDR.unpack_from(p, 0)
        for i in range(sc):
            codes.append(struct.unpack_from("<H", p, 32 + 2 * i)[0])
    dt = time.perf_counter() - t0
    print(f"    rate={n / dt / 1000:.1f} kS/s")
    return np.array(codes[:n], dtype=np.uint16)


def array_stats(codes: np.ndarray) -> tuple[int, float, float]:
    x = codes.astype(np.float64)
    med = int(np.median(x))
    std_uv = float(np.std((x - ADC_MID) * UV_PER_CODE))
    good = float(100.0 * np.mean(np.abs(x - np.median(x)) < 500))
    return med, std_uv, good


def parse_stats(stats_line: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("sample_clip", "rx_off", "spi_ovf", "usb_ovf"):
        m = re.search(rf"{key}=(\d+)", stats_line)
        if m:
            out[key] = int(m.group(1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Intan stream vs CONVERT")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--ch", type=int, default=2)
    ap.add_argument(
        "--lengths",
        default="256,1024,4096,8188",
        help="comma-separated sample counts",
    )
    ap.add_argument(
        "--cmds",
        default="SPI_STREAM_REAL,SPI_STREAM_REAL_SLOT",
        help="stream commands to test",
    )
    args = ap.parse_args()

    lengths = [int(x.strip()) for x in args.lengths.split(",") if x.strip()]
    cmds = [x.strip() for x in args.cmds.split(",") if x.strip()]
    fail = 0

    dev, _ = open_device(reset=not args.no_reset)
    try:
        prep(dev)
        c_med, c_std, c_n = convert_stats(dev, args.ch)
        print(f"CONVERT ch{args.ch} n={c_n}: med=0x{int(round(c_med)):04X} std={c_std:.1f} µV")
        if c_std > 50.0:
            print("  WARN: CONVERT std high (check GND / wiring)")
            fail += 1

        for cmd in cmds:
            print(f"\n--- {cmd} ---")
            for n in lengths:
                prep(dev)
                codes = stream_capture(dev, cmd, args.ch, n)
                med, std_uv, good = array_stats(codes)
                stats = run_text_command(dev, "STATS", drain_before=True)
                diag = parse_stats(stats)
                clip = diag.get("sample_clip", -1)
                print(
                    f"  n={n:5d} med=0x{med:04X} std={std_uv:7.1f} µV good={good:5.1f}% "
                    f"sample_clip={clip} rx_off={diag.get('rx_off', -1)}"
                )
                if good < 95.0 or std_uv > 100.0:
                    fail += 1
                if clip not in (-1, 0):
                    print("  FAIL: sample_clip > 0 (SPI re-entry before prior burst done)")
                    fail += 1
    finally:
        close_device(dev)

    print(f"\n{'PASS' if fail == 0 else f'FAIL ({fail} checks)'}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
