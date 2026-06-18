#!/usr/bin/env python3
"""
Moku Waveform Generator (Out1) → Intan elecN → запись SPI_STREAM_REAL.

Схема:
  Moku Out1 ── elecN ── 10 kΩ ── GND
  Moku GND ── общая земля с Intan

Moku amplitude = Vpp; на elec ≈ Vpp/2 peak. Линейный AC: Vpp ≤ ~8 mV.

  python3 tools/moku_sin_record_test.py --no-reset
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

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command  # noqa: E402

UV_PER_CODE = 0.195
ADC_MID = 32768.0
FRAME_HDR = struct.Struct("<IHHIIIIII")


def cmd(dev, text: str, *, timeout_ms: int = 120_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def prep_record(dev, ksps: int) -> None:
    cmd(dev, "STOP", timeout_ms=5000)
    cmd(dev, "WRITE 42 0 1 0")
    cmd(dev, f"INIT_RECORD {ksps}")
    cmd(dev, "CLEAR_ADC", timeout_ms=30000)
    cmd(dev, "READ 42", timeout_ms=5000)


def read_stream_ch(dev, ch: int, samples: int, *, timeout_ms: int) -> tuple[np.ndarray, float]:
    reply = run_text_command(
        dev, f"SPI_STREAM_REAL {samples} {ch} 0", timeout_ms=timeout_ms, drain_before=True
    )
    if not reply.startswith("OK"):
        raise RuntimeError(f"SPI_STREAM_REAL: {reply}")

    codes: list[int] = []
    t0 = time.perf_counter()
    while len(codes) < samples:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
        if len(pkt) != FRAME_SIZE:
            raise RuntimeError("short frame")
        magic, version, _flags, _seq, _first, sc, spi_ovf, usb_ovf, _meta = FRAME_HDR.unpack_from(pkt, 0)
        if magic != 0x52485331 or version != 1:
            raise RuntimeError("bad frame header")
        if spi_ovf or usb_ovf:
            raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
        for i in range(sc):
            if len(codes) >= samples:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])

    elapsed = time.perf_counter() - t0
    run_text_command(dev, "STOP", timeout_ms=10000, drain_before=False)
    return np.asarray(codes, dtype=np.uint16), elapsed


def analyze_sine(uv: np.ndarray, fs_hz: float, freq_hz: float) -> dict:
    x = uv.astype(np.float64) - float(np.mean(uv))
    n = len(x)
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs_hz)
    k = int(np.argmin(np.abs(freqs - freq_hz)))
    peak_bin = float(spec[k])
    k_dom = int(np.argmax(spec[1:]) + 1)
    dom_hz = float(freqs[k_dom])
    dom_uv = float(spec[k_dom] * 2.0 / n)
    noise = float(np.median(spec[1 : max(2, k // 2)])) if k > 2 else float(np.median(spec[1:]))
    return {
        "fs_hz": fs_hz,
        "peak_uv": float(np.max(np.abs(x))),
        "rms_uv": float(np.sqrt(np.mean(x * x))),
        "tone_bin_uv": peak_bin * 2.0 / n,
        "dom_hz": dom_hz,
        "dom_uv": dom_uv,
        "snr_db": float(20.0 * np.log10((peak_bin + 1e-12) / (noise + 1e-12))),
        "clip_frac": float(np.mean(np.abs(x) > 4500.0)),
        "freqs": freqs,
        "spec": spec,
    }


class MokuSine:
    def __init__(self, addr: str, out_channel: int) -> None:
        from moku.instruments import WaveformGenerator

        self.out_channel = out_channel
        self.wg = WaveformGenerator(addr, force_connect=True)

    def start(self, *, freq_hz: float, amp_vpp: float) -> None:
        if amp_vpp < 4e-3:
            print(f"WARN: Moku min Vpp=4 mV, using 4 mV (requested {amp_vpp*1e3:.2f} mV)")
            amp_vpp = 4e-3
        self.wg.generate_waveform(
            self.out_channel, "Sine", amplitude=amp_vpp, frequency=freq_hz, offset=0.0
        )
        time.sleep(0.05)

    def stop(self) -> None:
        self.wg.generate_waveform(self.out_channel, "Off")
        self.wg.relinquish_ownership()


def maybe_plot(
    t: np.ndarray,
    uv: np.ndarray,
    ana: dict,
    *,
    out: Path,
    ch: int,
    freq_hz: float,
    amp_vpp: float,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return

    x = uv.astype(np.float64) - float(np.mean(uv))
    fs = ana["fs_hz"]
    freq_hz = ana.get("_target_hz", freq_hz)

    periods_show = 6
    n_wave = min(len(t), max(500, int(periods_show / freq_hz * fs)))
    n_zoom = min(len(t), max(200, int(2.5 / freq_hz * fs)))

    fig, axes = plt.subplots(3, 1, figsize=(13, 9), layout="constrained")

    ax0, ax1, ax2 = axes
    ax0.plot(t[:n_wave] * 1e3, x[:n_wave], lw=0.7, color="#1f77b4", alpha=0.85)
    ax0.set_xlabel("Время, ms")
    ax0.set_ylabel("µV (AC)")
    ax0.set_title(
        f"Intan ch{ch} — sin {freq_hz:.0f} Hz, {periods_show} периодов, сырой сигнал  "
        f"(Moku Vpp={amp_vpp*1e3:.1f} mV)"
    )
    ax0.grid(True, alpha=0.35)

    ax1.plot(t[:n_zoom] * 1e3, x[:n_zoom], lw=1.0, color="#2ca02c", marker=".", ms=1.2)
    ax1.set_xlabel("Время, ms")
    ax1.set_ylabel("µV (AC)")
    tone = ana["tone_bin_uv"]
    dom = ana["dom_hz"]
    ax1.set_title(f"~2.5 периода (сырой)  |  FFT@{freq_hz:.0f}Hz≈{tone:.0f} µV  dom {dom:.0f} Hz")
    ax1.grid(True, alpha=0.35)

    f = ana["freqs"]
    s = ana["spec"] * 2.0 / len(uv)
    fmax_plot = min(500.0, fs / 2.0)
    mask = (f >= 0) & (f <= fmax_plot)
    ax2.plot(f[mask], s[mask], lw=0.9, color="#d62728")
    ax2.axvline(freq_hz, color="k", ls="--", alpha=0.6, label=f"Moku {freq_hz:.0f} Hz")
    if ana["dom_hz"] <= fmax_plot:
        ax2.axvline(ana["dom_hz"], color="#ff7f0e", ls=":", alpha=0.8, label=f"пик FFT {ana['dom_hz']:.0f} Hz")
    ax2.set_xlabel("Частота, Hz")
    ax2.set_ylabel("|FFT| µV")
    ax2.set_title(f"Спектр  |  SNR@{freq_hz:.0f}Hz≈{ana['snr_db']:.1f} dB  |  fs≈{fs/1e3:.0f} kS/s")
    ax2.set_xlim(0, fmax_plot)
    ax2.grid(True, alpha=0.35)
    ax2.legend(loc="upper right", fontsize=9)

    fig.suptitle(
        f"Moku Out1 → elec{ch} → Intan ADC  |  peak AC={ana['peak_uv']:.0f} µV  RMS={ana['rms_uv']:.0f} µV",
        fontsize=11,
    )
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"Plot: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku sine → Intan SPI_STREAM_REAL")
    ap.add_argument("--moku", default="mokugo-002464.local")
    ap.add_argument("--moku-out", type=int, default=1, help="WG Out1 = channel 1")
    ap.add_argument("--ch", type=int, default=2)
    ap.add_argument("--freq", type=float, default=50.0, help="Hz (50 Hz — самый чистый sin на bench)")
    ap.add_argument("--moku-amp-vpp", type=float, default=0.004, help="Vpp (min 4 mV)")
    ap.add_argument("--duration", type=float, default=0.5)
    ap.add_argument("--ksps", type=int, default=350000)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-moku", action="store_true")
    ap.add_argument("--plot", default="tools/moku_sin_record_ch2.png")
    args = ap.parse_args()

    samples = max(4096, int(args.duration * args.ksps))
    expect_peak_uv = args.moku_amp_vpp * 0.5e6

    print("=" * 60)
    print("Moku sine → Intan record")
    print("=" * 60)
    print(
        f"ch{args.ch}  Out{args.moku_out}  f={args.freq} Hz  "
        f"Vpp={args.moku_amp_vpp*1e3:.2f} mV  expect peak≈{expect_peak_uv:.0f} µV"
    )
    print(f"Capture: {samples} samples (~{args.duration} s)")
    print()

    moku: MokuSine | None = None
    if not args.no_moku:
        moku = MokuSine(args.moku, args.moku_out)
        moku.start(freq_hz=args.freq, amp_vpp=args.moku_amp_vpp)
        print("Moku WG: sine ON")

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        prep_record(dev, args.ksps)
        time.sleep(0.05)
        codes, elapsed = read_stream_ch(
            dev, args.ch, samples, timeout_ms=max(120_000, int(args.duration * 3000))
        )
        skip = min(len(codes) // 10, 8000)
        total = len(codes)
        fs = total / elapsed
        codes = codes[skip:]
        uv = (codes.astype(np.float64) - ADC_MID) * UV_PER_CODE
        t = np.arange(len(uv)) / fs
        ana = analyze_sine(uv, fs, args.freq)
        ana["_target_hz"] = args.freq

        print(f"Captured n={len(codes)}  wall={elapsed*1e3:.0f} ms  fs≈{fs/1e3:.1f} kS/s")
        print(f"Peak={ana['peak_uv']:.0f} µV  RMS={ana['rms_uv']:.0f} µV")
        print(f"Tone@{args.freq:.0f}Hz≈{ana['tone_bin_uv']:.0f} µV  dom≈{ana['dom_hz']:.0f}Hz ({ana['dom_uv']:.0f} µV)")
        print(f"SNR@{args.freq:.0f}Hz≈{ana['snr_db']:.1f} dB")
        if ana["clip_frac"] > 0.01:
            print(f"WARN: clip_frac={ana['clip_frac']*100:.1f}% — уменьшите Moku Vpp")
        ratio = ana["peak_uv"] / expect_peak_uv if expect_peak_uv > 0 else 0.0
        print(f"Peak ratio meas/expect ≈ {ratio:.2f}")
        # корреляция с эталонным sin (после прогрева)
        x_ac = uv.astype(np.float64) - float(np.mean(uv))
        t_corr = np.arange(len(x_ac)) / fs
        ref = np.sin(2 * np.pi * args.freq * t_corr)
        ref -= ref.mean()
        xs = x_ac - x_ac.mean()
        corr_p = float(np.corrcoef(xs, ref)[0, 1])
        corr_n = float(np.corrcoef(xs, -ref)[0, 1])
        print(f"corr sin={corr_p:+.3f}  corr(-sin)={corr_n:+.3f}")
        tone_ok = max(
            ana["tone_bin_uv"],
            ana["dom_uv"] if abs(ana["dom_hz"] - args.freq) < args.freq * 0.08 else 0,
        )
        ok = tone_ok > 200.0 and ana["snr_db"] > 8.0
        print("RESULT:", "OK" if ok else "FAIL")

        if args.plot:
            maybe_plot(
                t,
                uv,
                ana,
                out=Path(args.plot),
                ch=args.ch,
                freq_hz=args.freq,
                amp_vpp=args.moku_amp_vpp,
            )
        print(cmd(dev, "STATS", timeout_ms=10000))
    finally:
        close_device(dev, ifn)
        if moku is not None:
            moku.stop()
            print("Moku WG: Off")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
