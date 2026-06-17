#!/usr/bin/env python3
"""Sweep длительностей стим-импульсов ch2 + Moku Input1 (intan_pattern_testing_guide §10)."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402

SWEEP_ON_US = [500, 200, 100, 50, 20, 10]
CURRENT_UA = 180
CH = 2


def cmd(dev, text: str, *, timeout_ms: int = 120_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def prep_and_load_sweep(dev, ch: int, current_ua: int, on_us_list: list[int]) -> str:
    reg_val = 0x8000 | (current_ua & 0xFF)
    mask = 1 << ch
    neg = (0x80 << 24) | ((64 + ch) << 16) | reg_val
    pos = (0x80 << 24) | ((96 + ch) << 16) | reg_val

    cmd(dev, "INIT_STIM")
    cmd(dev, "WRITE 42 0 1 0")
    cmd(dev, "CLEAR_COMP")
    cmd(dev, f"WRITE {64 + ch} {reg_val:#x} 0 0")
    cmd(dev, f"WRITE {96 + ch} {reg_val:#x} 0 0")

    cmd(dev, "PATTERN_CLEAR")
    cmd(dev, f"PATTERN_ADD_RAW {neg:#x}")
    cmd(dev, f"PATTERN_ADD_RAW {pos:#x}")
    cmd(dev, f"PATTERN_ADD_WRITE 44 {mask} 0 0")
    for us in on_us_list:
        cmd(dev, f"PATTERN_ADD_WRITE 42 {mask} 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {us}")
        cmd(dev, "PATTERN_ADD_WRITE 42 0 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {us}")
    return cmd(dev, "PATTERN_STATUS")


def measure_pulses(t: np.ndarray, v: np.ndarray, thr: float) -> tuple[list[float], list[float], list[float]]:
    """Return (widths_us, rise_times_s, fall_times_s)."""
    high = v > thr
    if not np.any(high):
        return [], [], []

    edges = np.diff(high.astype(np.int8))
    rises = list(np.where(edges == 1)[0] + 1)
    falls = list(np.where(edges == -1)[0] + 1)
    if high[0]:
        rises.insert(0, 0)
    if high[-1]:
        falls.append(len(v) - 1)

    widths, rts, fts = [], [], []
    n = min(len(rises), len(falls))
    for i in range(n):
        if falls[i] > rises[i]:
            widths.append((t[falls[i]] - t[rises[i]]) * 1e6)
            rts.append(float(t[rises[i]]))
            fts.append(float(t[falls[i]]))
    return widths, rts, fts


def capture_during_run(moku_addr: str, scope_input: int, repeat: int, dev) -> tuple[np.ndarray, np.ndarray, float]:
    from moku.instruments import Oscilloscope

    osc = Oscilloscope(moku_addr, force_connect=True)
    try:
        src = f"Input{scope_input}"
        osc.set_defaults()
        osc.set_frontend(scope_input, "1MOhm", "DC", "10Vpp")
        osc.set_source(scope_input, src)
        osc.set_timebase(-0.001, 0.030, max_length=8192)
        osc.set_trigger(type="Edge", mode="Auto", edge="Rising", level=0.0, source=src)
        osc.set_acquisition_mode("Normal")

        best: dict[str, object] = {"score": -1.0}
        peaks: list[float] = []
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                try:
                    frame = osc.get_data(timeout=1, wait_reacquire=True, wait_complete=True)
                    v = np.asarray(frame[f"ch{scope_input}"], dtype=float)
                    t = np.asarray(frame["time"], dtype=float)
                    peak = float(np.max(v))
                    peaks.append(peak)
                    thr = 0.35
                    widths, _, _ = measure_pulses(t, v, thr)
                    score = len(widths) * 1000 + peak
                    if score > best["score"]:
                        best["score"] = score
                        best["t"] = t
                        best["v"] = v
                except Exception:
                    pass

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        t0 = time.time()
        cmd(dev, f"PATTERN_RUN {repeat}")
        cmd(dev, "WRITE 42 0 1 0")
        elapsed = time.time() - t0
        stop.set()
        poller.join(timeout=3.0)

        if peaks:
            print(f"Poll peaks: max={max(peaks):.3f} V, samples={len(peaks)}")
        if "t" not in best or best.get("score", -1) < 100:
            raise RuntimeError(
                f"Не удалось снять стим на Input{scope_input} "
                f"(peak poll={max(peaks) if peaks else 0:.3f} V)"
            )
        return best["t"], best["v"], elapsed
    finally:
        osc.relinquish_ownership()


def analyze_sweep(
    t: np.ndarray,
    v: np.ndarray,
    expected_on: list[int],
    repeat: int,
) -> dict:
    v_med = float(np.median(v))
    v_max = float(np.max(v))
    thr = v_med + 0.45 * (v_max - v_med) if v_max - v_med > 0.1 else 0.4

    widths, rises, falls = measure_pulses(t, v, thr)
    t0 = float(t[0])

    # группируем импульсы по повторам sweep (6 ON на повтор)
    n_on = len(expected_on)
    rows = []
    for i, w in enumerate(widths):
        rep = i // n_on
        idx = i % n_on
        if rep >= repeat:
            break
        exp = expected_on[idx]
        err = w - exp
        rows.append({"rep": rep + 1, "idx": idx + 1, "exp_us": exp, "meas_us": w, "err_us": err})

    # паузы между повторами паттерна: от падения последнего импульса sweep N до фронта первого sweep N+1
    gap_rows = []
    for rep in range(repeat - 1):
        last_i = (rep + 1) * n_on - 1
        first_next = (rep + 1) * n_on
        if last_i < len(falls) and first_next < len(rises):
            gap_us = (rises[first_next] - falls[last_i]) * 1e6
            gap_rows.append({"after_rep": rep + 1, "gap_us": gap_us})

    return {
        "threshold_v": thr,
        "v_max": v_max,
        "widths_us": widths,
        "pulse_rows": rows,
        "gap_rows": gap_rows,
        "t0": t0,
        "t_us": (t - t0) * 1e6,
        "v_mV": v * 1e3,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--moku", default="mokugo-002464.local")
    parser.add_argument("--scope-input", type=int, default=1)
    parser.add_argument("--ch", type=int, default=CH)
    parser.add_argument("--current-ua", type=int, default=CURRENT_UA)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--plot", default="tools/pattern_sweep_result.png")
    args = parser.parse_args()

    print("=" * 70)
    print("Sweep стим-импульсов (scope)")
    print("=" * 70)
    print(f"ch{args.ch}, {args.current_ua} µA, 10 kΩ, Input{args.scope_input}")
    print(f"ON widths (µs): {SWEEP_ON_US}  (OFF delay = ON)")
    print(f"PATTERN_RUN repeat: {args.repeat}")
    print()

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        status = prep_and_load_sweep(dev, args.ch, args.current_ua, SWEEP_ON_US)
        print("Pattern:", status)

        t, v, wall_s = capture_during_run(args.moku, args.scope_input, args.repeat, dev)
        print(f"Wall PATTERN_RUN {args.repeat}: {wall_s * 1e3:.1f} ms")
        print(f"Peak: {float(np.max(v)):.3f} V")
        print()

        res = analyze_sweep(t, v, SWEEP_ON_US, args.repeat)

        print(f"{'rep':>3} {'#':>2} {'exp_us':>7} {'meas_us':>9} {'err_us':>8} {'err%':>7}")
        print("-" * 42)
        for row in res["pulse_rows"]:
            err_pct = 100.0 * row["err_us"] / row["exp_us"]
            print(
                f"{row['rep']:3d} {row['idx']:2d} {row['exp_us']:7d} "
                f"{row['meas_us']:9.1f} {row['err_us']:+8.1f} {err_pct:+6.1f}%"
            )

        if res["gap_rows"]:
            print()
            print("Пауза между повторами патtern (последний OFF sweep → первый ON следующего):")
            print(f"{'after_rep':>10} {'gap_us':>10}")
            print("-" * 22)
            for g in res["gap_rows"]:
                print(f"{g['after_rep']:10d} {g['gap_us']:10.1f}")

        if args.plot:
            try:
                import matplotlib.pyplot as plt

                fig, ax = plt.subplots(figsize=(14, 4))
                ax.plot(res["t_us"], res["v_mV"], lw=0.7)
                ax.axhline(res["threshold_v"] * 1e3, color="r", ls="--", alpha=0.4)
                ax.set_xlabel("time (µs)")
                ax.set_ylabel("mV")
                ax.set_title(f"ch{args.ch} sweep ×{args.repeat}")
                ax.grid(True, alpha=0.3)
                fig.tight_layout()
                fig.savefig(args.plot, dpi=130)
                print(f"\nPlot: {args.plot}")
            except ImportError:
                pass

        for reg in (42, 40):
            print(cmd(dev, f"READ {reg}"))
    finally:
        close_device(dev, ifn)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
