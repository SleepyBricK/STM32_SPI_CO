#!/usr/bin/env python3
"""
Wall-clock test for STM32 PATTERN_ADD_DELAY_US scaling.

Loads patterns via USB and measures PATTERN_RUN duration vs repeat count.
If DWT delay matches SystemCoreClock (STATS sysclk_mhz), slope ≈ delay_us * repeat.

Usage:
  python3 tools/test_pattern_timing.py [--no-reset]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

sys.path.insert(0, "tools")
from usb_intan_lib import close_device, open_device, run_text_command

# Exact pattern from GUI screenshot (ch2, 180 µA, one 100 µs pulse, no trailing pause).
PATTERN_CH2_100US = [
    "PATTERN_ADD_RAW 0x804280B4",
    "PATTERN_ADD_RAW 0x806280B4",
    "PATTERN_ADD_RAW 0x802C0004",
    "PATTERN_ADD_RAW 0xA02A0004",
    "PATTERN_ADD_DELAY_US 100",
    "PATTERN_ADD_RAW 0xA02A0000",
]

# Two pulses: ON 100 OFF + pause 100 between (4 delays total if trailing kept — we omit last).
PATTERN_TWO_PULSE = [
    "PATTERN_ADD_RAW 0x802C0004",
    "PATTERN_ADD_RAW 0xA02A0004",
    "PATTERN_ADD_DELAY_US 100",
    "PATTERN_ADD_RAW 0xA02A0000",
    "PATTERN_ADD_DELAY_US 100",
    "PATTERN_ADD_RAW 0xA02A0004",
    "PATTERN_ADD_DELAY_US 100",
    "PATTERN_ADD_RAW 0xA02A0000",
]

# Delay-only baseline: one dummy RAW + 1000 µs delay per iteration.
PATTERN_DELAY_ONLY_1MS = [
    "PATTERN_ADD_RAW 0xA02A0000",
    "PATTERN_ADD_DELAY_US 1000",
]


def load_pattern(dev, lines: list[str]) -> str:
    run_text_command(dev, "PATTERN_CLEAR", timeout_ms=5000)
    for ln in lines:
        run_text_command(dev, ln, timeout_ms=5000)
    return run_text_command(dev, "PATTERN_STATUS", timeout_ms=5000)


def run_pattern(dev, repeat: int, timeout_ms: int) -> tuple[float, str]:
    t0 = time.perf_counter()
    reply = run_text_command(dev, f"PATTERN_RUN {repeat}", timeout_ms=timeout_ms)
    dt = time.perf_counter() - t0
    return dt, reply


def bench(dev, name: str, lines: list[str], repeats: list[int], delay_us_per_iter: int):
    print(f"\n=== {name} ===")
    status = load_pattern(dev, lines)
    print(f"  loaded: {status.strip()}")

    rows = []
    for rep in repeats:
        timeout_ms = max(60_000, rep * max(delay_us_per_iter, 100) // 1000 * 3 + 30_000)
        samples = []
        for _ in range(3):
            dt, reply = run_pattern(dev, rep, timeout_ms)
            if not reply.startswith("OK"):
                print(f"  ERR repeat={rep}: {reply}")
                break
            samples.append(dt)
        else:
            med = statistics.median(samples)
            per = med / rep * 1e6
            rows.append((rep, med, per))
            print(
                f"  repeat={rep:5d}  wall={med*1e3:8.2f} ms  "
                f"per_iter={per:8.1f} µs  (runs: {[f'{s*1e3:.2f}' for s in samples]})"
            )

    if len(rows) >= 2:
        r1, t1, _ = rows[0]
        r2, t2, _ = rows[-1]
        slope_us = (t2 - t1) / (r2 - r1) * 1e6
        print(f"  → slope (extra per repeat): {slope_us:.1f} µs/iter")
        print(f"  → expected delay-only part: ~{delay_us_per_iter} µs/iter (+ SPI fixed overhead)")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        stats = run_text_command(dev, "STATS", timeout_ms=5000)
        print(stats.strip())
        sysclk = 0
        for tok in stats.split():
            if tok.startswith("sysclk_mhz="):
                sysclk = int(tok.split("=", 1)[1])
        print(f"sysclk_mhz={sysclk}")

        run_text_command(dev, "INIT_STIM", timeout_ms=10000)
        run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000)
        run_text_command(dev, "CLEAR_COMP", timeout_ms=5000)

        bench(
            dev,
            "GUI pattern ch2 single pulse 100 µs",
            PATTERN_CH2_100US,
            repeats=[100, 500, 1000],
            delay_us_per_iter=100,
        )
        bench(
            dev,
            "Two pulses ON100 OFF100 ON100 (3×100µs delays)",
            PATTERN_TWO_PULSE,
            repeats=[100, 500],
            delay_us_per_iter=300,
        )
        bench(
            dev,
            "Baseline: 1000 µs delay only",
            PATTERN_DELAY_ONLY_1MS,
            repeats=[50, 200, 500],
            delay_us_per_iter=1000,
        )

        print("\nInterpretation:")
        print("  slope ≈ sum(PATTERN_ADD_DELAY_US) per iteration (+ ~fixed SPI/post-OFF)")
        print("  if slope ≈ delay/3  → DWT runs ~3× too fast (clock mismatch)")
        print("  if slope ≈ delay×3  → DWT runs ~3× too slow")
    finally:
        run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000)
        close_device(dev, ifn)


if __name__ == "__main__":
    main()
