#!/usr/bin/env python3
"""Moku sine → SPI_STREAM_FW RR8 (8 ch) @ 40 kS/s."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ch_fw_long_suite import capture_fw16, prep, plot_all16, plot_rms_envelope, skip_warmup, stats_line, uv  # noqa: E402
from fw_constants import N_CH  # noqa: E402
from moku_fw_sin_scan import corr_sin  # noqa: E402
from moku_sin_record_test import MokuSine, analyze_sine  # noqa: E402
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402


def plot_ch2_zoom(arrs: list[np.ndarray], ksps: int, freq: float, eff_f: float, out: Path, warmup_s: float) -> None:
    fs = ksps * 1000.0
    y = uv(skip_warmup(arrs[2], fs, warmup_s))
    n = min(len(y), int(5.0 / eff_f * fs))
    t = np.arange(n) / fs * 1000.0
    x = y[:n] - np.mean(y[:n])
    fig, axes = plt.subplots(2, 1, figsize=(14, 6), layout="constrained")
    axes[0].plot(t, x, lw=0.7, color="tab:green")
    axes[0].set_xlabel("ms")
    axes[0].set_ylabel("µV AC")
    axes[0].set_title(f"ch2 (Moku) — ~5 периодов @ {eff_f:.1f} Hz")
    axes[0].grid(True, alpha=0.3)

    spec = np.abs(np.fft.rfft(x)) * 2.0 / len(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fs)
    mask = freqs <= 200.0
    axes[1].plot(freqs[mask], spec[mask], lw=0.9, color="#d62728")
    axes[1].axvline(eff_f, color="k", ls="--", alpha=0.6, label=f"{eff_f:.1f} Hz")
    axes[1].set_xlabel("Hz")
    axes[1].set_ylabel("|FFT| µV")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"saved {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--moku", default="mokugo-002464.local")
    ap.add_argument("--moku-out", type=int, default=1)
    ap.add_argument("--moku-ch", type=int, default=2, help="канал Intan с Moku")
    ap.add_argument("--moku-amp-vpp", type=float, default=0.004)
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--ksps", type=int, default=40)
    ap.add_argument("--warmup-skip", type=float, default=0.5)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-moku", action="store_true")
    ap.add_argument("--out-prefix", default="tools/moku_fw8")
    args = ap.parse_args()

    n = int(args.duration * args.ksps * 1000)
    out_dir = Path(args.out_prefix).parent
    tag = f"{int(args.duration)}s_{args.ksps}ksps"

    moku: MokuSine | None = None
    c = 0.0
    ana = {"tone_bin_uv": 0.0}
    dev, ifn = open_device(reset=not args.no_reset)
    lines: list[str] = []
    try:
        if not args.no_moku:
            moku = MokuSine(args.moku, args.moku_out)
            moku.start(freq_hz=args.freq, amp_vpp=args.moku_amp_vpp)
            print(f"Moku Out{args.moku_out}: sin {args.freq} Hz  Vpp={args.moku_amp_vpp*1e3:.1f} mV → elec{args.moku_ch}")
            time.sleep(1.0)

        prep(dev)
        print(f"=== SPI_STREAM_FW 255  n={n}/ch  ksps={args.ksps} (~{args.duration}s) ===")
        t_cap0 = time.perf_counter()
        arrs, stats = capture_fw16(dev, n, args.ksps)
        wall_s = time.perf_counter() - t_cap0
        lines.append(f"Moku: f={args.freq}Hz Vpp={args.moku_amp_vpp*1e3:.1f}mV → ch{args.moku_ch}")
        for ch in range(N_CH):
            lines.append(stats_line(f"ch{ch} rr8", arrs[ch], stats, args.warmup_skip, args.ksps))

        fs_nom = args.ksps * 1000.0
        trimmed = skip_warmup(arrs[args.moku_ch], fs_nom, args.warmup_skip)
        wall_fs = len(trimmed) / wall_s if wall_s > 0.0 else fs_nom
        y = uv(trimmed)
        x = y - float(np.mean(y))
        spec = np.abs(np.fft.rfft(x)) * 2.0 / len(x)
        freqs = np.fft.rfftfreq(len(x), d=1.0 / wall_fs)
        k_dom = int(np.argmax(spec[1 : min(500, len(spec) // 2)]) + 1)
        eff_f = float(freqs[k_dom])
        dom_uv = float(spec[k_dom])
        ana = analyze_sine(y, wall_fs, eff_f)
        c = abs(corr_sin(y, wall_fs, eff_f))
        lines.append(f"capture wall={wall_s:.2f}s  fs_meas={wall_fs:.0f} Hz/ch  (nom {fs_nom:.0f})")
        lines.append(
            f"ch{args.moku_ch} sin: dom={eff_f:.2f}Hz tone={dom_uv:.0f}µV "
            f"|corr|={c:.3f} SNR={ana['snr_db']:.1f}dB clip_frac={ana['clip_frac']*100:.2f}%"
        )
        print("\n".join(lines[-(N_CH + 2) :]))
        print(stats)

        p_all = out_dir / f"{Path(args.out_prefix).name}_8ch_{tag}.png"
        p_rms = out_dir / f"{Path(args.out_prefix).name}_8ch_{tag}_rms.png"
        p_ch2 = out_dir / f"{Path(args.out_prefix).name}_ch2_{tag}.png"
        p_txt = out_dir / f"{Path(args.out_prefix).name}_{tag}_stats.txt"

        plot_all16(arrs, args.ksps, args.duration, p_all, args.warmup_skip)
        plot_rms_envelope(arrs, args.ksps, args.duration, p_rms, args.warmup_skip)
        plot_ch2_zoom(arrs, args.ksps, args.freq, eff_f, p_ch2, args.warmup_skip)
        p_txt.write_text("\n".join(lines) + "\n" + stats + "\n", encoding="utf-8")
        print(f"saved {p_all}")
        print(f"saved {p_rms}")
        print(f"saved {p_txt}")
    finally:
        close_device(dev, ifn)
        if moku is not None:
            moku.stop()
            print("Moku WG: Off")

    ok = c > 0.5 and dom_uv > 200
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
