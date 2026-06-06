#!/usr/bin/env python3
"""Захват SPI_STREAM_REAL (1ch), график сигнала и спектра."""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
UV_PER_LSB = 0.195
ADC_MID = 32768.0
SKIP_START = 5000


def read_stream(channel: int, n: int, reset: bool = True) -> tuple[np.ndarray, float, str]:
    dev, ifn = open_device(reset=reset)
    if reset:
        time.sleep(0.3)
    cmd = f"SPI_STREAM_REAL {n} {channel} 0"
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True).strip()
    if not reply.startswith("OK"):
        close_device(dev, ifn)
        raise RuntimeError(f"cmd failed: {reply}")
    t0 = time.perf_counter()
    codes: list[int] = []
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=600000))
        _, _, _, _, _, sc, _, _, _ = HDR.unpack_from(pkt, 0)
        for i in range(sc):
            if len(codes) >= n:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    close_device(dev, ifn)
    return np.array(codes, dtype=np.uint16), elapsed, stats


def codes_to_uv(codes: np.ndarray) -> np.ndarray:
    return (codes.astype(np.float64) - ADC_MID) * UV_PER_LSB


def plot_waveform_and_spectrum(
    uv: np.ndarray,
    fs_hz: float,
    channel: int,
    out_path: Path,
    *,
    skip: int = SKIP_START,
    spectrum_log: bool = False,
    fmax_hz: float = 10000.0,
) -> None:
    uv_use = uv[skip:]
    n = len(uv_use)
    t = (np.arange(n) + skip) / fs_hz

    # Спектр: Hann, односторонний, амплитуда µVrms по полосам
    win = np.hanning(n)
    xw = (uv_use - np.mean(uv_use)) * win
    spec = np.fft.rfft(xw)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs_hz)
    win_rms = np.sqrt(np.mean(win**2))
    mag = np.abs(spec) / (n * win_rms)
    mag[1:-1] *= 2.0

    f_hi = fs_hz / 2.0 if fmax_hz <= 0.0 else min(fmax_hz, fs_hz / 2.0)
    fmask = (freqs >= 0.0) & (freqs <= f_hi)

    rms = float(np.sqrt(np.mean(uv_use**2)))
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), layout="constrained")

    # Временная область: показываем до 3 с или всё, что есть
    show_s = min(3.0, t[-1] - t[0]) if n > 1 else 0.0
    mask_t = t <= (t[0] + show_s)
    axes[0].plot(t[mask_t], uv_use[mask_t], lw=0.4, color="#1f77b4")
    axes[0].set_xlabel("Время, с")
    axes[0].set_ylabel("µV")
    axes[0].set_title(
        f"ch{channel} SPI_STREAM_REAL  fs={fs_hz/1000:.1f} kHz  "
        f"RMS={rms:.1f} µV  (skip {skip} start)"
    )
    axes[0].grid(True, alpha=0.3)

    fx = freqs[fmask]
    fy = mag[fmask]
    if spectrum_log:
        pos = fy > 0.0
        axes[1].semilogx(fx[pos], 20.0 * np.log10(fy[pos]), lw=0.6, color="#d62728")
        axes[1].set_ylabel("Амплитуда, dBµV")
        axes[1].set_xlim(max(1.0, fx[1] if len(fx) > 1 else 1.0), f_hi)
        spec_title = "Спектр (Hann FFT, log f, dBµV)"
    else:
        axes[1].plot(fx, fy, lw=0.5, color="#d62728")
        axes[1].set_ylabel("Амплитуда, µV")
        axes[1].set_xlim(0.0, f_hi)
        spec_title = "Спектр (Hann FFT, линейная шкала)"
    axes[1].set_xlabel("Частота, Гц")
    axes[1].set_title(spec_title)
    axes[1].grid(True, alpha=0.3)

    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Захват 1ch и график + спектр")
    parser.add_argument("--channel", type=int, default=2)
    parser.add_argument("--duration", type=float, default=3.0, help="Длительность захвата, с")
    parser.add_argument("--rate", type=float, default=420000.0, help="Ожидаемый fs для расчёта N")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--spectrum-log",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Log шкала частот и dBµV (по умолчанию линейный спектр)",
    )
    parser.add_argument(
        "--fmax",
        type=float,
        default=10000.0,
        help="Верхняя частота спектра, Гц (0 = Nyquist)",
    )
    parser.add_argument(
        "--from-csv",
        type=Path,
        default=None,
        help="Построить из CSV (index raw uv), без USB-захвата",
    )
    args = parser.parse_args()

    if args.from_csv is not None:
        data = np.loadtxt(args.from_csv, skiprows=1)
        if data.ndim == 1:
            data = data.reshape(1, -1)
        codes = data[:, 1].astype(np.uint16)
        fs_hz = len(codes) / args.duration if args.duration > 0.0 else args.rate
        elapsed = len(codes) / fs_hz
        stats = f"from_csv={args.from_csv.name} n={len(codes)}"
        print(f"=== ch{args.channel} from CSV n={len(codes)} fs~{fs_hz/1000:.1f}kHz ===")
    else:
        n = int(args.duration * args.rate)
        n = max(n, 50000)
        print(f"=== ch{args.channel} capture ~{args.duration}s n={n} ===")
        codes, elapsed, stats = read_stream(args.channel, n, reset=args.reset)
        fs_hz = len(codes) / elapsed

    uv = codes_to_uv(codes)

    print(f"USB wall: {fs_hz/1000:.1f} kS/s  elapsed={elapsed:.2f}s")
    print(f"STATS: {stats}")
    print(f"RMS all={np.sqrt(np.mean(uv**2)):.2f} µV  skip={SKIP_START} "
          f"RMS={np.sqrt(np.mean(uv[SKIP_START:]**2)):.2f} µV")

    out = args.out or Path(__file__).resolve().parent / f"ch{args.channel}_stream_{args.duration:.0f}s.png"
    plot_waveform_and_spectrum(
        uv, fs_hz, args.channel, out, spectrum_log=args.spectrum_log, fmax_hz=args.fmax
    )

    if args.csv:
        np.savetxt(
            args.csv,
            np.column_stack([np.arange(len(uv)), codes, uv]),
            fmt=["%d", "%d", "%.6f"],
            header="index raw uv",
            comments="",
        )
        print(f"CSV: {args.csv}")

    print(f"PNG: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
