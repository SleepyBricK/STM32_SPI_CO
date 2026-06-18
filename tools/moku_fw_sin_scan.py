#!/usr/bin/env python3
"""
Moku WG OutN → Intan elec → SPI_STREAM_FW (solo ch).

Частотный скан: для каждой f — захват, FFT, corr с sin.

  python3 tools/moku_fw_sin_scan.py --ch 0 --moku-amp-vpp 0.2 --freqs 10,25,50,100 --no-reset
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from moku_sin_record_test import MokuSine, analyze_sine, UV_PER_CODE, ADC_MID  # noqa: E402
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command  # noqa: E402

FRAME_HDR = struct.Struct("<IHHIIIIII")


def cmd(dev, text: str, *, timeout_ms: int = 120_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def prep_fw(dev) -> None:
    cmd(dev, "STOP", timeout_ms=5000)
    cmd(dev, "INIT_RECORD 350000", timeout_ms=15000)
    cmd(dev, "CLEAR_ADC", timeout_ms=30000)


def capture_fw_solo(dev, ch: int, n: int, ksps: int, *, timeout_ms: int) -> tuple[np.ndarray, float]:
    reply = run_text_command(
        dev, f"SPI_STREAM_FW {n} {ch} 0 {ksps}", timeout_ms=timeout_ms, drain_before=True
    )
    if not reply.startswith("OK"):
        raise RuntimeError(reply)
    codes: list[int] = []
    t0 = time.perf_counter()
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
        _, _, _, _, _, sc, spi_ovf, usb_ovf, _ = FRAME_HDR.unpack_from(pkt, 0)
        if spi_ovf or usb_ovf:
            raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
        for i in range(sc):
            if len(codes) >= n:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    run_text_command(dev, "STOP", timeout_ms=10000, drain_before=False)
    return np.asarray(codes, dtype=np.uint16), elapsed


def corr_sin(uv: np.ndarray, fs: float, freq: float) -> float:
    x = uv.astype(np.float64) - float(np.mean(uv))
    t = np.arange(len(x)) / fs
    ref = np.sin(2 * np.pi * freq * t)
    ref -= ref.mean()
    xs = x - x.mean()
    cp = float(np.corrcoef(xs, ref)[0, 1])
    cn = float(np.corrcoef(xs, -ref)[0, 1])
    return cp if abs(cp) >= abs(cn) else cn


def parse_freqs(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def maybe_plot_scan(rows: list[dict], out: Path, ch: int, amp_vpp: float, ksps: int) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return

    freqs = [r["freq"] for r in rows]
    tone = [r["tone_uv"] for r in rows]
    peak = [r["peak_uv"] for r in rows]
    corr = [abs(r["corr"]) for r in rows]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    ax0, ax1, ax2, ax3 = axes.flat
    ax0.semilogy(freqs, tone, "o-", label="FFT tone")
    ax0.semilogy(freqs, peak, "s--", alpha=0.7, label="peak AC")
    ax0.set_xlabel("Hz")
    ax0.set_ylabel("µV")
    ax0.set_title(f"ch{ch}  Moku Vpp={amp_vpp*1e3:.0f} mV")
    ax0.grid(True, alpha=0.3)
    ax0.legend()

    ax1.plot(freqs, corr, "o-", color="#2ca02c")
    ax1.set_ylim(0, 1.05)
    ax1.set_xlabel("Hz")
    ax1.set_ylabel("|corr sin|")
    ax1.grid(True, alpha=0.3)

    best = max(rows, key=lambda r: r["tone_uv"])
    t = best["t_ms"]
    x = best["x_ac"]
    ax2.plot(t, x, lw=0.7)
    ax2.set_xlabel("ms")
    ax2.set_ylabel("µV AC")
    ax2.set_title(f"Waveform f={best['freq']:.0f} Hz  tone={best['tone_uv']:.0f} µV")
    ax2.grid(True, alpha=0.3)

    fsp = best["freqs"]
    spec = best["spec"]
    mask = fsp <= min(500.0, ksps * 500)
    ax3.plot(fsp[mask], spec[mask], lw=0.8)
    ax3.axvline(best["freq"], color="k", ls="--", alpha=0.6)
    ax3.set_xlabel("Hz")
    ax3.set_ylabel("|FFT| µV")
    ax3.set_title(f"Spectrum f={best['freq']:.0f} Hz  SNR={best['snr_db']:.1f} dB")
    ax3.grid(True, alpha=0.3)

    fig.suptitle(f"Moku Out → ch{ch}  SPI_STREAM_FW @ {ksps} kS/s", fontsize=12)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Plot: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku sine freq scan → SPI_STREAM_FW")
    ap.add_argument("--moku", default="mokugo-002464.local")
    ap.add_argument("--moku-out", type=int, default=1)
    ap.add_argument("--ch", type=int, default=0)
    ap.add_argument("--moku-amp-vpp", type=float, default=0.2, help="Moku amplitude = Vpp (200 mV = 0.2)")
    ap.add_argument("--freqs", default="10,25,50,100,200", help="Hz через запятую")
    ap.add_argument("--duration", type=float, default=1.0, help="с на частоту")
    ap.add_argument("--ksps", type=int, default=40)
    ap.add_argument("--warmup-skip", type=float, default=0.3)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-moku", action="store_true")
    ap.add_argument("--plot", default="tools/moku_fw_ch0_sin_scan.png")
    args = ap.parse_args()

    freqs = parse_freqs(args.freqs)
    n = max(4096, int(args.duration * args.ksps * 1000))
    expect_peak_uv = args.moku_amp_vpp * 0.5e6
    fs_nom = args.ksps * 1000.0

    print("=" * 60)
    print("Moku → SPI_STREAM_FW freq scan")
    print("=" * 60)
    print(
        f"ch{args.ch}  Out{args.moku_out}  Vpp={args.moku_amp_vpp*1e3:.0f} mV  "
        f"expect peak≈{expect_peak_uv:.0f} µV  ksps={args.ksps}"
    )
    if expect_peak_uv > 7000:
        print("WARN: Vpp >> ADC linear range (~12 mVpp) — ожидай клип/плоский верх sin")
    print(f"Freqs: {freqs}  n={n}/pt (~{args.duration}s)")
    print()

    moku: MokuSine | None = None
    dev, ifn = open_device(reset=not args.no_reset)
    rows: list[dict] = []
    try:
        prep_fw(dev)
        for freq in freqs:
            if moku is not None:
                moku.start(freq_hz=freq, amp_vpp=args.moku_amp_vpp)
            elif not args.no_moku:
                moku = MokuSine(args.moku, args.moku_out)
                moku.start(freq_hz=freq, amp_vpp=args.moku_amp_vpp)
                print("Moku WG: ON")
            else:
                pass

            time.sleep(0.15)
            codes, elapsed = capture_fw_solo(
                dev, args.ch, n, args.ksps, timeout_ms=max(120_000, int(args.duration * 4000))
            )
            prep_fw(dev)

            skip = int(args.warmup_skip * fs_nom)
            skip = min(skip, len(codes) // 4)
            codes = codes[skip:]
            uv = (codes.astype(np.float64) - ADC_MID) * UV_PER_CODE
            fs = len(codes) / elapsed if elapsed > 0 else fs_nom
            ana = analyze_sine(uv, fs, freq)
            c = corr_sin(uv, fs, freq)
            x_ac = uv.astype(np.float64) - float(np.mean(uv))
            periods = min(5, max(2, int(5.0 / freq)))
            n_show = min(len(uv), int(periods / freq * fs))
            row = {
                "freq": freq,
                "peak_uv": ana["peak_uv"],
                "rms_uv": ana["rms_uv"],
                "tone_uv": ana["tone_bin_uv"],
                "dom_hz": ana["dom_hz"],
                "snr_db": ana["snr_db"],
                "clip_frac": ana["clip_frac"],
                "corr": c,
                "fs": fs,
                "t_ms": np.arange(n_show) / fs * 1e3,
                "x_ac": x_ac[:n_show],
                "freqs": ana["freqs"],
                "spec": ana["spec"] * 2.0 / len(uv),
            }
            rows.append(row)
            clip_s = f"  clip={ana['clip_frac']*100:.1f}%" if ana["clip_frac"] > 0.001 else ""
            print(
                f"f={freq:6.1f} Hz  peak={ana['peak_uv']:7.0f} µV  "
                f"tone={ana['tone_bin_uv']:7.0f} µV  |corr|={abs(c):.3f}  "
                f"SNR={ana['snr_db']:5.1f} dB{clip_s}"
            )

        stats = run_text_command(dev, "STATS", timeout_ms=15000, drain_before=False).strip()
        print()
        print(stats)
        if args.plot:
            maybe_plot_scan(rows, Path(args.plot), args.ch, args.moku_amp_vpp, args.ksps)
    finally:
        close_device(dev, ifn)
        if moku is not None:
            moku.stop()
            print("Moku WG: Off")

    ok = any(r["tone_uv"] > 200 and abs(r["corr"]) > 0.5 for r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
