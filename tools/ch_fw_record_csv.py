#!/usr/bin/env python3
"""Запись SPI_STREAM_FW RR8 (8 каналов) → CSV + графики.

Production: 40 kS/s/ch, `SPI_STREAM_FW n 255 0 40`.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_fw_long_suite import (
    capture_fw16,
    plot_all16,
    plot_rms_envelope,
    prep,
    skip_warmup,
    uv,
)
from fw_constants import FW_KSPS_DEFAULT, N_CH, fw_stream_cmd
from usb_intan_lib import close_device, open_device, run_text_command

UV_PER_CODE = 0.195
ADC_MID = 32768.0
TESTING_DIR = Path(__file__).resolve().parent / "testing"


def write_csv(
    path: Path,
    arrs: list[np.ndarray],
    ksps: int,
    *,
    meta: str = "",
) -> int:
    """Wide CSV: sample, t_s, ch0_adc, ch0_uv, …, ch7_adc, ch7_uv. Returns row count."""
    n = min(len(a) for a in arrs)
    if n == 0:
        raise ValueError("empty capture")

    fs_hz = ksps * 1000.0
    t_s = np.arange(n, dtype=np.float64) / fs_hz

    header_cols = ["sample", "t_s"]
    data_cols: list[np.ndarray] = [np.arange(n, dtype=np.int64), t_s]

    for ch in range(N_CH):
        adc = arrs[ch][:n].astype(np.int32)
        uv = (adc.astype(np.float64) - ADC_MID) * UV_PER_CODE
        header_cols.extend([f"ch{ch}_adc", f"ch{ch}_uv"])
        data_cols.extend([adc, uv])

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if meta:
            for line in meta.strip().splitlines():
                f.write(f"# {line}\n")
        f.write(",".join(header_cols) + "\n")

    table = np.column_stack(data_cols)
    with path.open("ab") as f:
        np.savetxt(f, table, delimiter=",", fmt=["%d", "%.6f"] + ["%d", "%.4f"] * N_CH)

    return n


def parse_clip(stats: str) -> int:
    m = re.search(r"sample_clip=(\d+)", stats)
    return int(m.group(1)) if m else -1


def trim_arrays(arrs: list[np.ndarray], n: int) -> list[np.ndarray]:
    return [a[:n] for a in arrs]


def plot_ch_zoom(
    arrs: list[np.ndarray],
    ksps: int,
    out: Path,
    *,
    ch: int,
    t0_s: float,
    win_s: float,
    ylim_uv: float,
) -> None:
    fs = ksps * 1000.0
    i0 = max(0, int(t0_s * fs))
    i1 = min(len(arrs[ch]), i0 + max(1, int(win_s * fs)))
    seg = arrs[ch][i0:i1]
    t = np.arange(len(seg)) / fs + t0_s
    y = uv(seg)
    rms = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
    fig, ax = plt.subplots(figsize=(14, 3), layout="constrained")
    ax.plot(t, y, lw=0.5, color=f"C{ch % 10}")
    ax.axhline(0, color="gray", ls="--", lw=0.4)
    ax.set_xlim(t0_s, t0_s + win_s)
    ax.set_ylim(-ylim_uv, ylim_uv)
    ax.set_xlabel("с")
    ax.set_ylabel("µV")
    ax.set_title(f"ch{ch}  [{t0_s:.1f}…{t0_s + win_s:.1f}] s  RMS={rms:.0f} µV")
    ax.grid(True, alpha=0.25)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_all_ch_zoom(
    arrs: list[np.ndarray],
    ksps: int,
    out: Path,
    *,
    t0_s: float,
    win_s: float,
    ylim_uv: float,
) -> None:
    fs = ksps * 1000.0
    i0 = max(0, int(t0_s * fs))
    i1 = min(min(len(a) for a in arrs), i0 + max(1, int(win_s * fs)))
    fig, axes = plt.subplots(8, 1, figsize=(14, 16), layout="constrained", sharex=True)
    for ch, ax in enumerate(axes):
        seg = arrs[ch][i0:i1]
        t = np.arange(len(seg)) / fs + t0_s
        y = uv(seg)
        rms = float(np.sqrt(np.mean(y * y))) if len(y) else 0.0
        ax.plot(t, y, lw=0.5, color=f"C{ch % 10}")
        ax.axhline(0, color="gray", ls="--", lw=0.3)
        ax.set_ylim(-ylim_uv, ylim_uv)
        ax.set_ylabel("µV")
        ax.set_title(f"ch{ch}  RMS={rms:.0f} µV", loc="left", fontsize=9)
        ax.grid(True, alpha=0.2)
    axes[-1].set_xlabel("с")
    axes[-1].set_xlim(t0_s, t0_s + win_s)
    fig.suptitle(
        f"RR8 zoom ±{ylim_uv:.0f} µV  [{t0_s:.1f}…{t0_s + win_s:.1f}] s @ {ksps} kS/s/ch",
        fontsize=12,
    )
    fig.savefig(out, dpi=140)
    plt.close(fig)


def write_summary(
    path: Path,
    arrs: list[np.ndarray],
    ksps: int,
    *,
    meta_header: str,
    warmup_s: float,
) -> None:
    fs = ksps * 1000.0
    lines = [meta_header.rstrip(), "uv_per_code=0.195", "adc_mid=32768", "", " ch        n       med      med_uv      RMS µV"]
    lines.append("------------------------------------------------")
    n = min(len(a) for a in arrs)
    for ch in range(N_CH):
        trimmed = skip_warmup(arrs[ch][:n], fs, warmup_s)
        med = int(np.median(trimmed))
        med_uv = (med - ADC_MID) * UV_PER_CODE
        rms = float(np.sqrt(np.mean(uv(trimmed) ** 2)))
        lines.append(f"  {ch}  {len(trimmed):8d}  0x{med:04X}  {med_uv:9.1f}  {rms:9.1f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_plots(
    arrs: list[np.ndarray],
    ksps: int,
    duration_s: float,
    stem: Path,
    *,
    warmup_s: float,
    zoom_s: float,
    zoom_ylim_uv: float,
    zoom_t0_s: float,
) -> list[Path]:
    out_dir = stem.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    p_all = stem.with_name(stem.name + "_8ch.png")
    p_rms = stem.with_name(stem.name + "_rms.png")
    p_zoom = stem.with_name(stem.name + f"_ch2_zoom{zoom_s:g}s.png")
    p_all_z = stem.with_name(stem.name + "_all_ch_zoom1s.png")

    plot_all16(arrs, ksps, duration_s, p_all, warmup_s)
    plot_rms_envelope(arrs, ksps, duration_s, p_rms, warmup_s)
    plot_ch_zoom(
        arrs,
        ksps,
        p_zoom,
        ch=2,
        t0_s=zoom_t0_s,
        win_s=zoom_s,
        ylim_uv=zoom_ylim_uv,
    )
    plot_all_ch_zoom(
        arrs,
        ksps,
        p_all_z,
        t0_s=zoom_t0_s,
        win_s=zoom_s,
        ylim_uv=zoom_ylim_uv,
    )

    paths = [p_all, p_rms, p_zoom, p_all_z]
    for ch in range(N_CH):
        p_ch = out_dir / f"ch{ch}_zoom{zoom_s:g}s.png"
        plot_ch_zoom(
            arrs,
            ksps,
            p_ch,
            ch=ch,
            t0_s=zoom_t0_s,
            win_s=zoom_s,
            ylim_uv=zoom_ylim_uv,
        )
        paths.append(p_ch)
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description="RR8 SPI_STREAM_FW → CSV (8 channels)")
    ap.add_argument("--duration", type=float, default=10.0, help="секунд на канал")
    ap.add_argument(
        "--ksps",
        type=int,
        default=FW_KSPS_DEFAULT,
        help=f"kS/s на канал (production: {FW_KSPS_DEFAULT})",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="выходной CSV (default: tools/testing/fw_rr8_<duration>s_<ksps>ksps.csv)",
    )
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument(
        "--warmup-skip",
        type=float,
        default=0.5,
        help="секунд прогрева HPF — только для графиков (CSV полный)",
    )
    ap.add_argument(
        "--zoom-s",
        type=float,
        default=1.0,
        help="длина zoom-графика (с)",
    )
    ap.add_argument(
        "--zoom-t0",
        type=float,
        default=0.5,
        help="начало окна zoom-графиков (с)",
    )
    ap.add_argument(
        "--zoom-ylim-uv",
        type=float,
        default=250.0,
        help="полуширина оси Y на zoom-графиках (µV)",
    )
    ap.add_argument("--no-plots", action="store_true", help="только CSV, без PNG")
    args = ap.parse_args()

    if args.ksps != FW_KSPS_DEFAULT:
        print(f"warning: production rate is {FW_KSPS_DEFAULT} kS/s/ch; requested {args.ksps}")

    n_per_ch = max(1, int(args.duration * args.ksps * 1000))
    out = args.output
    if out is None:
        tag = f"{int(args.duration)}s_{args.ksps}ksps"
        out = TESTING_DIR / f"fw_rr8_{tag}.csv"

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    try:
        prep(dev)
        cmd = fw_stream_cmd(n_per_ch, args.ksps)
        print(f"=== {cmd}  (~{args.duration}s, {N_CH} ch) ===")
        t0 = time.perf_counter()
        arrs, stats = capture_fw16(dev, n_per_ch, args.ksps)
        wall_s = time.perf_counter() - t0

        clip = parse_clip(stats)
        meta_block = (
            f"cmd={cmd}\n"
            f"ksps_per_ch={args.ksps}\n"
            f"duration_s={args.duration}\n"
            f"channels=0..{N_CH - 1}\n"
            f"wall_s={wall_s:.3f}\n"
            f"sample_clip={clip}\n"
            f"stats={stats}\n"
            f"utc={datetime.now(timezone.utc).isoformat()}"
        )
        n_written = write_csv(
            out,
            arrs,
            args.ksps,
            meta=(
                meta_block
                + f"\nuv_per_code={UV_PER_CODE}\nadc_mid={int(ADC_MID)}"
            ),
        )

        ch_ksps = n_written / wall_s / 1000.0
        print(f"rows={n_written}  wall={wall_s:.2f}s  rate={ch_ksps:.1f} kS/s/ch  clip={clip}")
        print(f"saved {out}  ({out.stat().st_size // 1024} KiB)")

        plot_arrs = trim_arrays(arrs, n_written)

        if not args.no_plots:
            stem = out.with_suffix("")
            zoom_s = min(args.zoom_s, args.duration - args.zoom_t0)
            if zoom_s <= 0.0:
                zoom_s = min(args.zoom_s, args.duration)
            pngs = save_plots(
                plot_arrs,
                args.ksps,
                args.duration,
                stem,
                warmup_s=args.warmup_skip,
                zoom_s=zoom_s,
                zoom_ylim_uv=args.zoom_ylim_uv,
                zoom_t0_s=args.zoom_t0,
            )
            p_summary = stem.with_name(stem.name + "_summary.txt")
            write_summary(
                p_summary,
                plot_arrs,
                args.ksps,
                meta_header=meta_block,
                warmup_s=args.warmup_skip,
            )
            print(f"saved {p_summary}")
            for p in pngs:
                print(f"saved {p}")

        if clip != 0:
            print("warning: sample_clip != 0", file=sys.stderr)
            return 1
        return 0
    finally:
        try:
            run_text_command(dev, "STOP", drain_before=True)
        except Exception:
            pass
        close_device(dev, ifn)


if __name__ == "__main__":
    raise SystemExit(main())
