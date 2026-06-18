#!/usr/bin/env python3
"""
Moku WG → Intan SPI_STREAM_FW RR8 (8 ch).

Waveforms: square (лучше всего виден), sine, dc.

  python3 tools/moku_fw_wg_record.py --moku-ch 4 --waveform square --freq 5 --moku-amp-vpp 0.008 --no-reset
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ch_fw_long_suite import capture_fw16, prep, plot_all16, plot_rms_envelope, skip_warmup, stats_line, uv  # noqa: E402
from fw_constants import N_CH  # noqa: E402
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402


class MokuWg:
    def __init__(self, addr: str, out_channel: int) -> None:
        from moku.instruments import WaveformGenerator

        self.out_channel = out_channel
        self.wg = WaveformGenerator(addr, force_connect=True)

    def start(
        self,
        *,
        waveform: str,
        amp_vpp: float,
        freq_hz: float = 5.0,
        offset_v: float = 0.0,
        duty: float = 50.0,
    ) -> None:
        wf = waveform.lower()
        if amp_vpp < 4e-3 and wf != "dc":
            print(f"WARN: Moku min Vpp=4 mV → using 4 mV (requested {amp_vpp*1e3:.2f} mV)")
            amp_vpp = 4e-3
        if wf == "dc":
            self.wg.generate_waveform(self.out_channel, "DC", offset=offset_v)
        elif wf == "square":
            self.wg.generate_waveform(
                self.out_channel,
                "Square",
                amplitude=amp_vpp,
                frequency=freq_hz,
                offset=offset_v,
                duty=duty,
            )
        elif wf == "sine":
            self.wg.generate_waveform(
                self.out_channel, "Sine", amplitude=amp_vpp, frequency=freq_hz, offset=offset_v
            )
        else:
            raise ValueError(f"unsupported waveform: {waveform}")
        time.sleep(0.05)

    def stop(self) -> None:
        self.wg.generate_waveform(self.out_channel, "Off")
        self.wg.relinquish_ownership()


def analyze_square(y: np.ndarray, fs: float, freq: float) -> dict:
    x = y.astype(np.float64) - float(np.mean(y))
    ptp = float(np.percentile(x, 99) - np.percentile(x, 1))
    peak = float(np.max(np.abs(x)))
    # оценка частоты по zero-crossings
    s = np.sign(x)
    zc = np.where(np.diff(s) != 0)[0]
    if len(zc) >= 4:
        half_periods = np.diff(zc) / fs
        est_f = 1.0 / (2.0 * float(np.median(half_periods)))
    else:
        est_f = float("nan")
    hi = float(np.percentile(x, 90))
    lo = float(np.percentile(x, 10))
    mid = (hi + lo) * 0.5
    sep = hi - lo
    return {
        "ptp_uv": ptp,
        "peak_uv": peak,
        "hi_uv": hi,
        "lo_uv": lo,
        "sep_uv": sep,
        "est_f_hz": est_f,
        "target_f_hz": freq,
        "clip_frac": float(np.mean(np.abs(x) > 6000.0)),
    }


def plot_moku_ch(
    arrs: list[np.ndarray],
    moku_ch: int,
    ksps: int,
    wall_fs: float,
    warmup_s: float,
    ana: dict,
    waveform: str,
    out: Path,
) -> None:
    y = uv(skip_warmup(arrs[moku_ch], ksps * 1000.0, warmup_s))
    n = min(len(y), int(wall_fs * 3.0))  # ~3 s
    y = y[:n]
    t = np.arange(n) / wall_fs
    fig, ax = plt.subplots(figsize=(14, 4), layout="constrained")
    ax.plot(t, y, lw=0.5, color="tab:purple")
    ax.axhline(0, color="gray", ls="--", lw=0.4)
    if waveform == "square" and ana.get("sep_uv", 0) > 50:
        ax.axhline(ana["hi_uv"], color="#2ca02c", ls=":", lw=0.8, alpha=0.7)
        ax.axhline(ana["lo_uv"], color="#d62728", ls=":", lw=0.8, alpha=0.7)
    title = f"ch{moku_ch}  {waveform}  ptp={ana.get('ptp_uv', 0):.0f} µV"
    if "sep_uv" in ana:
        title += f"  Δ={ana['sep_uv']:.0f} µV"
    if not np.isnan(ana.get("est_f_hz", float("nan"))):
        title += f"  fest≈{ana['est_f_hz']:.1f} Hz"
    ax.set_xlabel("с")
    ax.set_ylabel("µV")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"saved {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku WG → SPI_STREAM_FW RR8")
    ap.add_argument("--moku", default="mokugo-002464.local")
    ap.add_argument("--moku-out", type=int, default=1)
    ap.add_argument("--moku-ch", type=int, default=4)
    ap.add_argument("--waveform", choices=("square", "sine", "dc"), default="square")
    ap.add_argument("--moku-amp-vpp", type=float, default=0.008)
    ap.add_argument("--moku-dc-offset", type=float, default=0.002, help="V, для dc или offset square/sine")
    ap.add_argument("--freq", type=float, default=5.0, help="Hz (square/sine); 5 Hz хорошо виден на 5 s")
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--ksps", type=int, default=40)
    ap.add_argument("--warmup-skip", type=float, default=0.5)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-moku", action="store_true")
    ap.add_argument("--out-prefix", default="tools/moku_fw8_sq")
    args = ap.parse_args()

    n = int(args.duration * args.ksps * 1000)
    out_dir = Path(args.out_prefix).parent
    tag = f"{args.waveform}_ch{args.moku_ch}_{int(args.duration)}s_{args.ksps}ksps"

    moku: MokuWg | None = None
    dev, ifn = open_device(reset=not args.no_reset)
    lines: list[str] = []
    ana: dict = {}
    try:
        if not args.no_moku:
            moku = MokuWg(args.moku, args.moku_out)
            moku.start(
                waveform=args.waveform,
                amp_vpp=args.moku_amp_vpp,
                freq_hz=args.freq,
                offset_v=args.moku_dc_offset,
            )
            if args.waveform == "dc":
                print(
                    f"Moku Out{args.moku_out}: DC offset={args.moku_dc_offset*1e3:.2f} mV → elec{args.moku_ch}"
                )
            else:
                print(
                    f"Moku Out{args.moku_out}: {args.waveform} {args.freq} Hz  "
                    f"Vpp={args.moku_amp_vpp*1e3:.1f} mV → elec{args.moku_ch}"
                )
            time.sleep(1.0)

        prep(dev)
        print(f"=== SPI_STREAM_FW 255  n={n}/ch  ksps={args.ksps} ===")
        t0 = time.perf_counter()
        arrs, stats = capture_fw16(dev, n, args.ksps)
        wall_s = time.perf_counter() - t0

        lines.append(
            f"Moku {args.waveform} → ch{args.moku_ch}  "
            f"Vpp={args.moku_amp_vpp*1e3:.1f}mV f={args.freq}Hz"
        )
        for ch in range(N_CH):
            lines.append(stats_line(f"ch{ch} rr8", arrs[ch], stats, args.warmup_skip, args.ksps))

        trimmed = skip_warmup(arrs[args.moku_ch], args.ksps * 1000.0, args.warmup_skip)
        wall_fs = len(trimmed) / wall_s if wall_s > 0.0 else args.ksps * 1000.0
        y = uv(trimmed)
        if args.waveform == "square":
            ana = analyze_square(y, wall_fs, args.freq)
            lines.append(
                f"ch{args.moku_ch} square: ptp={ana['ptp_uv']:.0f}µV sep={ana['sep_uv']:.0f}µV "
                f"fest≈{ana['est_f_hz']:.2f}Hz clip={ana['clip_frac']*100:.2f}%"
            )
        else:
            x = y - float(np.mean(y))
            ana = {
                "ptp_uv": float(np.percentile(x, 99) - np.percentile(x, 1)),
                "peak_uv": float(np.max(np.abs(x))),
                "sep_uv": 0.0,
                "est_f_hz": float("nan"),
            }
            lines.append(
                f"ch{args.moku_ch}: ptp={ana['ptp_uv']:.0f}µV peak={ana['peak_uv']:.0f}µV"
            )
        lines.append(f"wall={wall_s:.2f}s fs_meas={wall_fs:.0f}Hz/ch")
        print("\n".join(lines[-(N_CH + 3) :]))
        print(stats)

        prefix = Path(args.out_prefix).name
        p_all = out_dir / f"{prefix}_8ch_{tag}.png"
        p_rms = out_dir / f"{prefix}_8ch_{tag}_rms.png"
        p_ch = out_dir / f"{prefix}_ch{args.moku_ch}_{tag}.png"
        p_txt = out_dir / f"{prefix}_{tag}_stats.txt"

        plot_all16(arrs, args.ksps, args.duration, p_all, args.warmup_skip)
        plot_rms_envelope(arrs, args.ksps, args.duration, p_rms, args.warmup_skip)
        plot_moku_ch(arrs, args.moku_ch, args.ksps, wall_fs, args.warmup_skip, ana, args.waveform, p_ch)
        p_txt.write_text("\n".join(lines) + "\n" + stats + "\n", encoding="utf-8")
        print(f"saved {p_all}")
        print(f"saved {p_txt}")
    finally:
        close_device(dev, ifn)
        if moku is not None:
            moku.stop()
            print("Moku WG: Off")

    ok = ana.get("sep_uv", 0) > 300 or ana.get("ptp_uv", 0) > 300
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
