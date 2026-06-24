#!/usr/bin/env python3
"""Health check + scan 8 каналов @ SPI_STREAM_FW 40 kS/s/ch.

Проверяет PING/ID/STATS, затем короткий solo-захват по каждому каналу и RR8 сводку.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import capture_fw16, capture_single, prep, skip_warmup, uv
from fw_constants import FW_KSPS_DEFAULT, N_CH, fw_stream_cmd
from usb_intan_lib import close_device, open_device, run_text_command

ADC_MID = 32768.0


@dataclass
class ChResult:
    ch: int
    n: int
    med: int
    rms_uv: float
    clip: int
    rate_ksps: float
    mode: str


def parse_clip(stats: str) -> int:
    m = re.search(r"sample_clip=(\d+)", stats)
    return int(m.group(1)) if m else -1


def ch_stats(codes: np.ndarray, ksps: int, warmup_s: float = 0.5) -> tuple[int, float, int]:
    fs = ksps * 1000.0
    trimmed = skip_warmup(codes, fs, warmup_s)
    med = int(np.median(trimmed))
    rms = float(np.sqrt(np.mean(uv(trimmed) ** 2)))
    return med, rms, len(trimmed)


def health_check(dev) -> dict[str, str]:
    out: dict[str, str] = {}
    for cmd in ("PING", "ID", "STATS"):
        out[cmd] = run_text_command(dev, cmd, drain_before=True).strip()
    return out


def scan_solo(dev, ch: int, n: int, ksps: int, warmup_s: float) -> ChResult:
    prep(dev)
    t0 = time.perf_counter()
    codes, stats = capture_single(dev, ch, n, ksps)
    wall = time.perf_counter() - t0
    med, rms, n_use = ch_stats(codes, ksps, warmup_s)
    return ChResult(
        ch=ch,
        n=n_use,
        med=med,
        rms_uv=rms,
        clip=parse_clip(stats),
        rate_ksps=len(codes) / wall / 1000.0,
        mode="solo",
    )


def scan_rr8(dev, n: int, ksps: int, warmup_s: float) -> tuple[list[ChResult], str]:
    prep(dev)
    t0 = time.perf_counter()
    arrs, stats = capture_fw16(dev, n, ksps)
    wall = time.perf_counter() - t0
    clip = parse_clip(stats)
    rate = min(len(a) for a in arrs) / wall / 1000.0
    rows: list[ChResult] = []
    for ch in range(N_CH):
        med, rms, n_use = ch_stats(arrs[ch], ksps, warmup_s)
        rows.append(
            ChResult(
                ch=ch,
                n=n_use,
                med=med,
                rms_uv=rms,
                clip=clip,
                rate_ksps=rate,
                mode="rr8",
            )
        )
    return rows, stats


def plot_scan(solo: list[ChResult], rr8: list[ChResult], ksps: int, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout="constrained")
    x = np.arange(N_CH)
    for ax, rows, title in (
        (axes[0], solo, f"solo @ {ksps} kS/s"),
        (axes[1], rr8, f"RR8 @ {ksps} kS/s"),
    ):
        rms = [r.rms_uv for r in rows]
        bars = ax.bar(x, rms, width=0.6, color="steelblue")
        ax.set_xticks(x)
        ax.set_xticklabels([f"ch{i}" for i in range(N_CH)])
        ax.set_ylabel("RMS µV")
        ax.set_title(title)
        ax.grid(True, axis="y", alpha=0.3)
        for bar, r in zip(bars, rows):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height(),
                f"0x{r.med:04X}",
                ha="center",
                va="bottom",
                fontsize=7,
                rotation=0,
            )
    fig.suptitle("SPI_STREAM_FW channel scan", fontsize=12)
    fig.savefig(out, dpi=140)
    plt.close(fig)


def print_table(rows: list[ChResult], title: str) -> None:
    print(f"\n{title}")
    print(f"{'ch':>3}  {'n':>7}  {'med':>8}  {'RMS µV':>10}  {'rate':>8}  {'clip':>6}")
    print("-" * 52)
    for r in rows:
        print(
            f"{r.ch:3d}  {r.n:7d}  0x{r.med:04X}  {r.rms_uv:10.1f}  "
            f"{r.rate_ksps:7.1f}  {r.clip:6d}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Health + 8ch scan @ SPI_STREAM_FW")
    ap.add_argument("--ksps", type=int, default=FW_KSPS_DEFAULT)
    ap.add_argument("--solo-s", type=float, default=0.0, help="solo capture per channel (s); 0=skip")
    ap.add_argument("--rr8-s", type=float, default=3.0, help="RR8 capture (s)")
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--warmup-skip", type=float, default=0.5)
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("-o", "--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args()

    if args.ksps != FW_KSPS_DEFAULT:
        print(f"warning: production rate is {FW_KSPS_DEFAULT} kS/s/ch")

    n_solo = max(500, int(args.solo_s * args.ksps * 1000))
    n_rr8 = max(1000, int(args.rr8_s * args.ksps * 1000))
    fail = 0

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    try:
        print("=== Health ===")
        hc = health_check(dev)
        for k, v in hc.items():
            print(f"{k}: {v}")
        if hc.get("PING") != "PONG":
            print("FAIL: PING", file=sys.stderr)
            return 1
        if not hc.get("ID", "").startswith("OK"):
            print("FAIL: ID", file=sys.stderr)
            return 1
        m = re.search(r"sysclk_mhz=(\d+)", hc.get("STATS", ""))
        if m and int(m.group(1)) != 480:
            print(f"WARN: sysclk_mhz={m.group(1)} (expected 480)")

        solo_rows: list[ChResult] = []
        if args.solo_s > 0.0:
            print(f"\n=== Solo scan ({n_solo}/ch, {args.ksps} kS/s) ===")
            for ch in range(N_CH):
                r = scan_solo(dev, ch, n_solo, args.ksps, args.warmup_skip)
                solo_rows.append(r)
                ok = r.clip == 0 and r.rate_ksps >= args.ksps * 0.85
                flag = "OK" if ok else "WARN"
                print(
                    f"  ch{ch}: med=0x{r.med:04X} RMS={r.rms_uv:.0f}µV "
                    f"rate={r.rate_ksps:.1f} clip={r.clip} [{flag}]"
                )
                if r.clip != 0:
                    fail += 1
        else:
            print("\n=== Solo scan: skipped (production RR8 only) ===")

        print(f"\n=== RR8 {fw_stream_cmd(n_rr8, args.ksps)} ===")
        rr8_rows, stats = scan_rr8(dev, n_rr8, args.ksps, args.warmup_skip)
        print(f"STATS: {stats}")
        print_table(rr8_rows, "RR8 per-channel")
        if rr8_rows[0].clip != 0:
            fail += 1
        if rr8_rows[0].rate_ksps < args.ksps * 0.85:
            fail += 1
            print(f"WARN: RR8 rate {rr8_rows[0].rate_ksps:.1f} < {args.ksps * 0.85:.1f} kS/s")

        txt = args.output_dir / f"fw_ch_scan_{int(args.rr8_s)}s_{args.ksps}ksps.txt"
        lines = [f"{k}: {v}" for k, v in hc.items()]
        lines.append("")
        for r in solo_rows:
            lines.append(
                f"solo ch{r.ch}: n={r.n} med=0x{r.med:04X} rms={r.rms_uv:.1f}µV "
                f"rate={r.rate_ksps:.1f} clip={r.clip}"
            )
        lines.append("")
        for r in rr8_rows:
            lines.append(
                f"rr8 ch{r.ch}: n={r.n} med=0x{r.med:04X} rms={r.rms_uv:.1f}µV "
                f"rate={r.rate_ksps:.1f} clip={r.clip}"
            )
        lines.append(f"stats={stats}")
        txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nsaved {txt}")

        if not args.no_plot and rr8_rows:
            png = args.output_dir / f"fw_ch_scan_{int(args.rr8_s)}s_{args.ksps}ksps.png"
            if solo_rows:
                plot_scan(solo_rows, rr8_rows, args.ksps, png)
            else:
                plot_scan(rr8_rows, rr8_rows, args.ksps, png.with_name(png.stem + "_rr8.png"))
                png = png.with_name(png.stem + "_rr8.png")
            print(f"saved {png}")

        print(f"\n{'PASS' if fail == 0 else f'DONE with {fail} warning(s)'}")
        return 0 if fail == 0 else 1
    finally:
        try:
            run_text_command(dev, "STOP", drain_before=True)
        except Exception:
            pass
        close_device(dev, ifn)


if __name__ == "__main__":
    raise SystemExit(main())
