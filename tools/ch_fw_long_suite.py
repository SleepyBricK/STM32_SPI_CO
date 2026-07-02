#!/usr/bin/env python3
"""Длинный захват SPI_STREAM_FW: 8 каналов (ch=255) + ch2 solo, сравнение."""

from __future__ import annotations

import re
import struct
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_intan_lib import (
    EP_IN,
    FRAME_SIZE,
    Rhs1FwDecodeState,
    close_device,
    iter_rhs1_fw_samples,
    open_device,
    run_text_command,
    validate_rhs1_frame,
)

UV = 0.195
MID = 32768.0
from fw_constants import N_CH, FW_KSPS_DEFAULT


def prep(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "INIT_RECORD 350000", drain_before=True)
    run_text_command(dev, "CLEAR_ADC", timeout_ms=30000, drain_before=True)


def capture_single(dev, ch: int, n: int, ksps: int) -> tuple[np.ndarray, str]:
    cmd = f"SPI_STREAM_FW {n} {ch} 0 {ksps}"
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True).strip()
    if not reply.startswith("OK"):
        raise RuntimeError(f"single cmd failed: {reply}")
    codes: list[int] = []
    expected_seq = 0
    deadline = time.perf_counter() + max(300.0, n / max(ksps, 1) * 2.0 + 120.0)
    while len(codes) < n:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"single ch{ch}: got {len(codes)}/{n}")
        p = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60000))
        _, _, _, seq, _, sc, _, _, _ = validate_rhs1_frame(p, expected_seq)
        expected_seq = seq + 1
        for i in range(sc):
            codes.append(struct.unpack_from("<H", p, 32 + 2 * i)[0])
    stats = run_text_command(dev, "STATS", timeout_ms=15000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    return np.array(codes[:n], dtype=np.uint16), stats


def capture_fw16(dev, n_per_ch: int, ksps: int) -> tuple[list[np.ndarray], str]:
    total = n_per_ch * N_CH
    cmd = f"SPI_STREAM_FW {n_per_ch} 255 0 {ksps}"
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True).strip()
    if not reply.startswith("OK"):
        raise RuntimeError(f"fw16 cmd failed: {reply}")
    buckets: list[list[int]] = [[] for _ in range(N_CH)]
    state = Rhs1FwDecodeState(strict_seq=True)
    deadline = time.perf_counter() + max(300.0, (n_per_ch / max(ksps, 1)) * 2.0 + 120.0)
    while state.global_idx < total:
        if time.perf_counter() > deadline:
            raise TimeoutError(f"fw16: got {state.global_idx}/{total}")
        p = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60000))
        for ch, adc in iter_rhs1_fw_samples(p, state, n_ch=N_CH):
            if state.global_idx > total:
                break
            if ch < N_CH:
                buckets[ch].append(adc)
    stats = run_text_command(dev, "STATS", timeout_ms=15000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    return [np.array(b, dtype=np.uint16) for b in buckets], stats


def uv(codes: np.ndarray) -> np.ndarray:
    return (codes.astype(np.float64) - MID) * UV


def skip_warmup(codes: np.ndarray, fs: float, warmup_s: float) -> np.ndarray:
    n_skip = int(warmup_s * fs)
    if n_skip <= 0 or n_skip >= len(codes):
        return codes
    return codes[n_skip:]


def stats_line(name: str, codes: np.ndarray, stats: str, warmup_s: float = 0.0, ksps: int = 1) -> str:
    fs = ksps * 1000.0
    trimmed = skip_warmup(codes, fs, warmup_s) if warmup_s > 0.0 else codes
    med = int(np.median(trimmed))
    rms = float(np.sqrt(np.mean(uv(trimmed) ** 2)))
    clip = re.search(r"sample_clip=(\d+)", stats)
    clip_v = clip.group(1) if clip else "?"
    suffix = f" (после {warmup_s:.1f}s skip)" if warmup_s > 0.0 else ""
    return f"{name}{suffix}: med=0x{med:04X} RMS={rms:.0f}µV n={len(trimmed)} clip={clip_v}"


def sliding_rms(y: np.ndarray, fs: float, win_s: float = 0.2, step_s: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    win = max(1, int(win_s * fs))
    step = max(1, int(step_s * fs))
    if len(y) < win:
        return np.array([]), np.array([])
    rms_vals: list[float] = []
    times: list[float] = []
    for i in range(0, len(y) - win + 1, step):
        seg = y[i : i + win]
        rms_vals.append(float(np.sqrt(np.mean(seg * seg))))
        times.append((i + win // 2) / fs)
    return np.array(times), np.array(rms_vals)


def plot_all16(
    arrs: list[np.ndarray], ksps: int, duration_s: float, out: Path, warmup_s: float = 0.5
) -> None:
    fig, axes = plt.subplots(4, 2, figsize=(16, 14), layout="constrained")
    fs = ksps * 1000.0
    for ch, ax in enumerate(axes.flat):
        trimmed = skip_warmup(arrs[ch], fs, warmup_s)
        y = uv(trimmed)
        t = np.arange(len(y)) / fs + warmup_s
        med = int(np.median(trimmed))
        rms = float(np.sqrt(np.mean(y * y)))
        ax.plot(t, y, lw=0.25, color=f"C{ch % 10}")
        ax.axhline(0, color="gray", ls="--", lw=0.3)
        ax.axvline(warmup_s, color="orange", ls=":", lw=0.5, alpha=0.7)
        ax.set_title(f"ch{ch}  med=0x{med:04X}  RMS={rms:.0f} µV")
        ax.set_xlabel("с")
        ax.set_ylabel("µV")
        ax.grid(True, alpha=0.2)
    skip_note = f", первые {warmup_s:.1f} s отброшены" if warmup_s > 0.0 else ""
    fig.suptitle(
        f"SPI_STREAM_FW 255  @ {ksps} kS/s/ch  ≈{duration_s:.0f} s{skip_note}",
        fontsize=13,
    )
    fig.savefig(out, dpi=140)
    plt.close(fig)


def plot_rms_envelope(
    arrs: list[np.ndarray], ksps: int, duration_s: float, out: Path, warmup_s: float = 0.0
) -> None:
    fig, axes = plt.subplots(4, 2, figsize=(16, 14), layout="constrained")
    fs = ksps * 1000.0
    for ch, ax in enumerate(axes.flat):
        y = uv(arrs[ch])
        t_rms, rms = sliding_rms(y, fs, win_s=0.2, step_s=0.05)
        if len(t_rms) == 0:
            continue
        ax.plot(t_rms, rms, lw=1.0, color=f"C{ch % 10}")
        if warmup_s > 0.0:
            ax.axvline(warmup_s, color="orange", ls=":", lw=0.8, alpha=0.8)
        ax.set_title(f"ch{ch}  sliding RMS 200 ms")
        ax.set_xlabel("с")
        ax.set_ylabel("µV RMS")
        ax.grid(True, alpha=0.2)
    fig.suptitle(
        f"SPI_STREAM_FW 255 — sliding RMS (200 ms окно, шаг 50 ms)  ≈{duration_s:.0f} s @ {ksps} kS/s",
        fontsize=12,
    )
    fig.savefig(out, dpi=140)
    plt.close(fig)


def plot_ch2_compare(solo: np.ndarray, from16: np.ndarray, ksps: int, out: Path) -> None:
    fs = ksps * 1000.0
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), layout="constrained", sharex=True)
    for ax, data, title in (
        (axes[0], solo, "ch2 solo  SPI_STREAM_FW n 2 0"),
        (axes[1], from16, "ch2 из режима 255 (8 каналов)"),
    ):
        y = uv(data)
        t = np.arange(len(y)) / fs
        med = int(np.median(data))
        rms = float(np.sqrt(np.mean(y * y)))
        ax.plot(t, y, lw=0.3, color="tab:green")
        ax.axhline(0, color="gray", ls="--", lw=0.4)
        ax.set_ylabel("µV")
        ax.set_title(f"{title}   med=0x{med:04X}  RMS={rms:.0f} µV")
        ax.grid(True, alpha=0.2)
    axes[1].set_xlabel("с")
    fig.suptitle(f"ch2 GND 10 kΩ — сравнение solo vs RR16  ({len(solo)/fs:.1f} s @ {ksps} kS/s)", fontsize=12)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_ch2_zoom(solo: np.ndarray, from16: np.ndarray, ksps: int, zoom_s: float, out: Path) -> None:
    fs = ksps * 1000.0
    n = int(zoom_s * fs)
    fig, ax = plt.subplots(figsize=(14, 4), layout="constrained")
    t = np.arange(n) / fs
    ax.plot(t, uv(solo[:n]), lw=0.6, label="solo", alpha=0.9)
    ax.plot(t, uv(from16[:n]), lw=0.6, label="из RR16", alpha=0.7)
    ax.axhline(0, color="gray", ls="--", lw=0.4)
    ax.set_xlabel("с")
    ax.set_ylabel("µV")
    ax.set_title(f"ch2 zoom {zoom_s}s @ {ksps} kS/s")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.25)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=5.0, help="секунд на канал")
    ap.add_argument("--ksps", type=int, default=40, help="kS/s per channel (production: 40)")
    ap.add_argument("--warmup-skip", type=float, default=0.5, help="секунд прогрева, отбрасываемых на графике/trace")
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    n = int(args.duration * args.ksps * 1000)
    if n < 100:
        n = 100
    wall_timeout = max(300.0, args.duration / max(args.ksps, 1) * 8.0 * N_CH + 120.0)
    out_dir = Path(__file__).resolve().parent
    lines: list[str] = []

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.4)
    try:
        prep(dev)
        print(f"=== ch2 solo  n={n} ksps={args.ksps} (~{args.duration}s) ===")
        solo, st_solo = capture_single(dev, 2, n, args.ksps)
        lines.append(stats_line("ch2 solo", solo, st_solo, args.warmup_skip, args.ksps))

        prep(dev)
        print(f"=== FW8  n={n}/ch ksps={args.ksps} (~{args.duration}s) ===")
        arrs, st16 = capture_fw16(dev, n, args.ksps)
        lines.append(stats_line("ch2 rr8", arrs[2], st16, args.warmup_skip, args.ksps))
        for ch in range(N_CH):
            lines.append(stats_line(f"ch{ch} rr8", arrs[ch], st16, args.warmup_skip, args.ksps))
    finally:
        close_device(dev, ifn)

    txt = out_dir / f"ch_fw_long_{int(args.duration)}s_stats.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:3]))
    print(f"... ({len(lines)} lines) -> {txt}")

    p_all = out_dir / f"ch_fw8_{int(args.duration)}s_{args.ksps}ksps.png"
    p_rms = out_dir / f"ch_fw8_{int(args.duration)}s_{args.ksps}ksps_rms.png"
    p_cmp = out_dir / f"ch2_fw_solo_vs_rr8_{int(args.duration)}s.png"
    p_z = out_dir / f"ch2_fw_zoom_1s_{int(args.duration)}s.png"

    plot_all16(arrs, args.ksps, args.duration, p_all, args.warmup_skip)
    plot_rms_envelope(arrs, args.ksps, args.duration, p_rms, args.warmup_skip)
    plot_ch2_compare(solo, arrs[2], args.ksps, p_cmp)
    plot_ch2_zoom(solo, arrs[2], args.ksps, min(1.0, args.duration), p_z)

    print(f"saved {p_all}")
    print(f"saved {p_rms}")
    print(f"saved {p_cmp}")
    print(f"saved {p_z}")
    print(f"wall_timeout budget ~{wall_timeout:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
