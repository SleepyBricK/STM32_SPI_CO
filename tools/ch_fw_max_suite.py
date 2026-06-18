#!/usr/bin/env python3
"""SPI_STREAM_FW_MAX: 8-ch free-run capture + plots."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import N_CH, prep, stats_line, uv
from ch_fw_max_bench import capture_fw_max
from usb_intan_lib import close_device, open_device

from fw_constants import N_CH


def plot_all16(arrs: list[np.ndarray], ch_ksps: float, duration_s: float, out: Path) -> None:
    fig, axes = plt.subplots(4, 2, figsize=(16, 14), layout="constrained")
    fs = ch_ksps * 1000.0
    for ch, ax in enumerate(axes.flat):
        y = uv(arrs[ch])
        t = np.arange(len(y)) / fs
        med = int(np.median(arrs[ch]))
        rms = float(np.sqrt(np.mean(y * y)))
        ax.plot(t, y, lw=0.25, color=f"C{ch % 10}")
        ax.axhline(0, color="gray", ls="--", lw=0.3)
        ax.set_title(f"ch{ch}  med=0x{med:04X}  RMS={rms:.0f} µV")
        ax.set_xlabel("с")
        ax.set_ylabel("µV")
        ax.grid(True, alpha=0.2)
    fig.suptitle(
        f"SPI_STREAM_FW_MAX 255  ~{ch_ksps:.1f} kS/s/ch  ≈{duration_s:.1f} s  (free-run)",
        fontsize=13,
    )
    fig.savefig(out, dpi=140)
    plt.close(fig)


def plot_ch0_ch2(arrs: list[np.ndarray], ch_ksps: float, clip: int, out: Path) -> None:
    fs = ch_ksps * 1000.0
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), layout="constrained", sharex=True)
    for ax, ch, label in (
        (axes[0], 0, "ch0 → GND 1 MΩ"),
        (axes[1], 2, "ch2 → GND 10 kΩ"),
    ):
        y = uv(arrs[ch])
        t = np.arange(len(y)) / fs
        med = int(np.median(arrs[ch]))
        rms = float(np.sqrt(np.mean(y * y)))
        ax.plot(t, y, lw=0.2, color="tab:blue" if ch == 0 else "tab:green")
        ax.axhline(0, color="gray", ls="--", lw=0.4)
        ax.set_ylabel("µV")
        ax.set_title(f"{label}  med=0x{med:04X}  RMS={rms:.0f} µV")
        ax.grid(True, alpha=0.2)
    axes[1].set_xlabel("с")
    fig.suptitle(
        f"SPI_STREAM_FW_MAX  clip={clip}  ~{ch_ksps:.1f} kS/s/ch  nss_midi=4",
        fontsize=12,
    )
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_ch2_zoom(arrs: list[np.ndarray], ch_ksps: float, zoom_s: float, out: Path) -> None:
    fs = ch_ksps * 1000.0
    n = min(len(arrs[2]), max(1, int(zoom_s * fs)))
    t = np.arange(n) / fs
    y = uv(arrs[2][:n])
    fig, ax = plt.subplots(figsize=(14, 4), layout="constrained")
    ax.plot(t, y, lw=0.5, color="tab:green")
    ax.axhline(0, color="gray", ls="--", lw=0.4)
    ax.set_xlabel("с")
    ax.set_ylabel("µV")
    rms = float(np.sqrt(np.mean(y * y)))
    ax.set_title(f"ch2 zoom {zoom_s}s  RMS={rms:.0f} µV  @ {ch_ksps:.1f} kS/s")
    ax.grid(True, alpha=0.25)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="SPI_STREAM_FW_MAX 16-ch capture + plots")
    ap.add_argument("--duration", type=float, default=5.0, help="целевая длительность на канал (с)")
    ap.add_argument(
        "--n",
        type=int,
        default=None,
        help="samples/ch (default: duration × est 30 kS/s)",
    )
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    n = args.n if args.n is not None else int(args.duration * 30_000)
    if n < 1000:
        n = 1000

    out_dir = Path(__file__).resolve().parent
    lines: list[str] = []

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        import time

        time.sleep(0.3)
    try:
        prep(dev)
        print(f"=== SPI_STREAM_FW_MAX  n={n}/ch  (target ~{args.duration}s) ===")
        arrs, wall_s, stats = capture_fw_max(dev, n, 255)
    finally:
        close_device(dev, ifn)

    ch_ksps = n / wall_s / 1000.0
    agg_ksps = ch_ksps * N_CH
    clip_m = re.search(r"sample_clip=(\d+)", stats)
    usb_m = re.search(r"usb_ovf=(\d+)", stats)
    clip = int(clip_m.group(1)) if clip_m else -1
    usb_ovf = int(usb_m.group(1)) if usb_m else -1
    duration_s = n / (ch_ksps * 1000.0) if ch_ksps > 0 else 0.0

    for ch in range(N_CH):
        lines.append(stats_line(f"ch{ch} max", arrs[ch], stats))
    txt = out_dir / f"ch_fw_max_{int(duration_s)}s_stats.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tag = f"{int(duration_s)}s"
    p_all = out_dir / f"ch_fw_max8_{tag}.png"
    p_gnd = out_dir / f"ch0_ch2_gnd_max_{tag}.png"
    p_z = out_dir / f"ch2_fw_max_zoom_1s_{tag}.png"

    plot_all16(arrs, ch_ksps, duration_s, p_all)
    plot_ch0_ch2(arrs, ch_ksps, clip, p_gnd)
    plot_ch2_zoom(arrs, ch_ksps, min(1.0, duration_s), p_z)

    print(f"SPI_STREAM_FW_MAX  n={n}/ch  wall={wall_s:.2f}s")
    print(f"  per-ch kS/s={ch_ksps:.1f}  aggregate={agg_ksps:.1f}  clip={clip}  usb_ovf={usb_ovf}")
    print(stats_line("ch0", arrs[0], stats))
    print(stats_line("ch2", arrs[2], stats))
    print(f"stats -> {txt}")
    print(f"saved {p_all}")
    print(f"saved {p_gnd}")
    print(f"saved {p_z}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
