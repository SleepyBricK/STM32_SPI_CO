#!/usr/bin/env python3
"""Тест C из intan_pattern_testing_guide.md — паттерн + Moku Go."""

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

# ch2 @ 10 kΩ: 180 µA → ~1.8 V (в гайде опечатка «1.8 mV»)
DEFAULT_CURRENT_UA = 180
LOAD_OHMS = 10_000.0


def cmd(dev, text: str, *, timeout_ms: int = 5000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def stim_raw_words(ch: int, current_ua: int) -> tuple[int, int]:
    """RAW только для регистров тока (не triggered)."""
    reg_val = 0x8000 | (current_ua & 0xFF)
    neg_reg = 64 + ch
    pos_reg = 96 + ch
    return (
        (0x80 << 24) | (neg_reg << 16) | reg_val,
        (0x80 << 24) | (pos_reg << 16) | reg_val,
    )


def load_single_pulse_pattern(dev, ch: int, current_ua: int, on_us: int) -> str:
    neg, pos = stim_raw_words(ch, current_ua)
    mask = 1 << ch
    cmd(dev, "PATTERN_CLEAR")
    cmd(dev, f"PATTERN_ADD_RAW {neg:#x}")
    cmd(dev, f"PATTERN_ADD_RAW {pos:#x}")
    cmd(dev, f"PATTERN_ADD_WRITE 44 {mask} 0 0")
    cmd(dev, f"PATTERN_ADD_WRITE 42 {mask} 1 0")
    cmd(dev, f"PATTERN_ADD_DELAY_US {on_us}")
    cmd(dev, "PATTERN_ADD_WRITE 42 0 1 0")
    return cmd(dev, "PATTERN_STATUS")


def load_burst_pattern(dev, ch: int, current_ua: int, on_us: int, off_us: int, count: int) -> str:
    neg, pos = stim_raw_words(ch, current_ua)
    mask = 1 << ch
    cmd(dev, "PATTERN_CLEAR")
    cmd(dev, f"PATTERN_ADD_RAW {neg:#x}")
    cmd(dev, f"PATTERN_ADD_RAW {pos:#x}")
    cmd(dev, f"PATTERN_ADD_WRITE 44 {mask} 0 0")
    for _ in range(count):
        cmd(dev, f"PATTERN_ADD_WRITE 42 {mask} 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {on_us}")
        cmd(dev, "PATTERN_ADD_WRITE 42 0 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {off_us}")
    return cmd(dev, "PATTERN_STATUS")


def load_duration_sweep(dev, ch: int, current_ua: int, durations_us: list[int]) -> str:
    neg, pos = stim_raw_words(ch, current_ua)
    mask = 1 << ch
    cmd(dev, "PATTERN_CLEAR")
    cmd(dev, f"PATTERN_ADD_RAW {neg:#x}")
    cmd(dev, f"PATTERN_ADD_RAW {pos:#x}")
    cmd(dev, f"PATTERN_ADD_WRITE 44 {mask} 0 0")
    for us in durations_us:
        cmd(dev, f"PATTERN_ADD_WRITE 42 {mask} 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {us}")
        cmd(dev, "PATTERN_ADD_WRITE 42 0 1 0")
        cmd(dev, f"PATTERN_ADD_DELAY_US {us}")
    return cmd(dev, "PATTERN_STATUS")


def measure_high_pulses(time_s: np.ndarray, volts: np.ndarray, threshold_v: float) -> list[float]:
    high = volts > threshold_v
    if not np.any(high):
        return []
    edges = np.diff(high.astype(np.int8))
    rises = np.where(edges == 1)[0] + 1
    falls = np.where(edges == -1)[0] + 1
    if high[0]:
        rises = np.insert(rises, 0, 0)
    if high[-1]:
        falls = np.append(falls, len(high) - 1)
    widths = []
    for i in range(min(len(rises), len(falls))):
        if falls[i] > rises[i]:
            widths.append(float(time_s[falls[i]] - time_s[rises[i]]))
    return widths


class MokuCapture:
    def __init__(self, addr: str, input_channel: int) -> None:
        from moku.instruments import Oscilloscope

        self.addr = addr
        self.input_channel = input_channel
        self.osc = Oscilloscope(addr, force_connect=True)

    def close(self) -> None:
        self.osc.relinquish_ownership()

    def configure(
        self,
        *,
        t_before_s: float,
        t_after_s: float,
        max_points: int,
        trigger_level_v: float,
        trigger_mode: str = "Normal",
    ) -> None:
        self._t_before = t_before_s
        self._t_after = t_after_s
        self._max_points = max_points
        src = f"Input{self.input_channel}"
        self.osc.set_defaults()
        self.osc.set_frontend(self.input_channel, "1MOhm", "DC", "10Vpp")
        self.osc.set_source(self.input_channel, src)
        self.osc.set_timebase(t_before_s, t_after_s, max_length=max_points)
        self.osc.set_trigger(
            type="Edge",
            mode=trigger_mode,
            edge="Rising",
            level=trigger_level_v,
            source=src,
        )
        self.osc.set_acquisition_mode("Normal")

    def capture_blocking(self, timeout_s: float = 30.0) -> tuple[np.ndarray, np.ndarray]:
        frame = self.osc.get_data(
            timeout=timeout_s, wait_reacquire=True, wait_complete=True
        )
        key = f"ch{self.input_channel}"
        return np.asarray(frame["time"], dtype=float), np.asarray(frame[key], dtype=float)

    def snap_ptp(self) -> float:
        _, v = self.capture_blocking(timeout_s=10.0)
        return float(np.max(v) - np.min(v))


def detect_scope_input(moku_addr: str, dev, ch: int, current_ua: int) -> int:
    """Выбирает Moku Input с максимальным сигналом при ручном ON."""
    mask = 1 << ch
    reg_val = 0x8000 | (current_ua & 0xFF)
    cmd(dev, f"WRITE {64 + ch} {reg_val:#x} 0 0")
    cmd(dev, f"WRITE {96 + ch} {reg_val:#x} 0 0")
    cmd(dev, f"WRITE 44 {mask} 0 0")
    cmd(dev, "WRITE 42 0 1 0")
    cmd(dev, "CLEAR_COMP")

    best_inp, best_ptp = 1, 0.0
    moku = MokuCapture(moku_addr, 1)
    try:
        for inp in (1, 2):
            moku.input_channel = inp
            moku.configure(
                t_before_s=-0.005,
                t_after_s=0.005,
                max_points=2048,
                trigger_level_v=0.0,
                trigger_mode="Auto",
            )
            cmd(dev, f"WRITE 42 {mask} 1 0")
            time.sleep(0.05)
            ptp = moku.snap_ptp()
            cmd(dev, "WRITE 42 0 1 0")
            time.sleep(0.01)
            print(f"  Input{inp} ptp={ptp * 1e3:.1f} mV")
            if ptp > best_ptp:
                best_ptp, best_inp = ptp, inp
    finally:
        moku.close()

    if best_ptp < 0.05:
        raise RuntimeError(
            "На Input1/Input2 нет стим-сигнала (>50 mV). "
            "Проверьте 10 kΩ между elec/ch и GND, stim_en."
        )
    return best_inp


def prep_stim(dev, ch: int, current_ua: int) -> None:
    reg_val = 0x8000 | (current_ua & 0xFF)
    cmd(dev, "INIT_STIM")
    cmd(dev, "WRITE 42 0 1 0")
    cmd(dev, "CLEAR_COMP")
    cmd(dev, f"WRITE {64 + ch} {reg_val:#x} 0 0")
    cmd(dev, f"WRITE {96 + ch} {reg_val:#x} 0 0")


def run_synced_capture(
    moku: MokuCapture,
    dev,
    repeat: int,
    *,
    strategy: str = "trigger",
    mid_sample_s: float = 0.35,
    run_timeout_ms: int = 60_000,
) -> tuple[np.ndarray, np.ndarray]:
    if strategy == "mid":
        moku.configure(
            t_before_s=moku._t_before,
            t_after_s=moku._t_after,
            max_points=moku._max_points,
            trigger_level_v=0.0,
            trigger_mode="Auto",
        )

        def run_pat() -> None:
            cmd(dev, f"PATTERN_RUN {repeat}", timeout_ms=run_timeout_ms)
            cmd(dev, "WRITE 42 0 1 0")

        th = threading.Thread(target=run_pat, daemon=True)
        th.start()
        time.sleep(mid_sample_s)
        t, v = moku.capture_blocking(timeout_s=10.0)
        th.join(timeout=run_timeout_ms / 1000 + 5)
        return t, v

    # poll: надёжный путь — снимки Auto во время блокирующего PATTERN_RUN
    best: dict[str, object] = {"max": -1.0}

    def poll() -> None:
        moku.configure(
            t_before_s=moku._t_before,
            t_after_s=moku._t_after,
            max_points=moku._max_points,
            trigger_level_v=0.0,
            trigger_mode="Auto",
        )
        while not poll_stop.is_set():
            try:
                t, v = moku.capture_blocking(timeout_s=3.0)
                peak = float(np.max(v))
                if peak > best["max"]:
                    best["max"] = peak
                    best["t"] = t
                    best["v"] = v
            except Exception:
                pass
            time.sleep(0.03)

    poll_stop = threading.Event()
    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    time.sleep(0.1)
    cmd(dev, f"PATTERN_RUN {repeat}", timeout_ms=run_timeout_ms)
    cmd(dev, "WRITE 42 0 1 0")
    poll_stop.set()
    poller.join(timeout=5.0)

    if "t" not in best:
        raise RuntimeError("scope poll: no frame captured")
    print(f"Run: OK PATTERN_RUN (peak {best['max']:.3f} V)")
    return best["t"], best["v"]


def analyze_pulses(
    t: np.ndarray,
    v: np.ndarray,
    expected_on_us: list[int],
    *,
    threshold_v: float = -1.0,
    tolerance_pct: float = 15.0,
) -> bool:
    v_ptp = float(np.max(v) - np.min(v))
    v_med = float(np.median(v))
    print(f"Signal: median={v_med * 1e3:.1f} mV  ptp={v_ptp * 1e3:.1f} mV")

    if threshold_v < 0:
        threshold_v = v_med * 0.5 if abs(v_med) > 0.05 else v_med + max(3.0 * float(np.std(v)), 0.05)
    print(f"Threshold: {threshold_v * 1e3:.1f} mV")

    widths_us = [w * 1e6 for w in measure_high_pulses(t, v, threshold_v)]
    print(f"Pulses detected: {len(widths_us)}")
    if not widths_us:
        print("ERR no pulses")
        return False

    n_cmp = min(len(widths_us), len(expected_on_us))
    print(f"\n{'#':>3}  {'meas_us':>9}  {'exp_us':>7}  {'err_us':>8}  {'err%':>7}")
    print("-" * 44)
    ok = True
    for i in range(n_cmp):
        exp = expected_on_us[i]
        meas = widths_us[i]
        err = meas - exp
        err_pct = 100.0 * err / exp if exp else 0.0
        flag = "OK" if abs(err_pct) <= tolerance_pct else "FAIL"
        ok = ok and flag == "OK"
        print(f"{i+1:3d}  {meas:9.1f}  {exp:7d}  {err:+8.1f}  {err_pct:+6.1f}%  {flag}")
    if len(widths_us) > n_cmp:
        print(f"... +{len(widths_us) - n_cmp} extra pulses")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Тест C: паттерн Intan + Moku Go")
    parser.add_argument("--moku", default="mokugo-002464.local")
    parser.add_argument("--scope-input", type=int, default=1, choices=(0, 1, 2),
                        help="Moku analog input (default: 1 = первый вход)")
    parser.add_argument("--ch", type=int, default=2, help="Intan stim channel (default: 2)")
    parser.add_argument("--current-ua", type=int, default=DEFAULT_CURRENT_UA)
    parser.add_argument(
        "--mode",
        choices=("pulse100", "burst", "sweep", "cal"),
        default="cal",
        help="pulse100=§7.3; burst=10×100µs; sweep=§10; cal=500µs then sweep",
    )
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--plot", default="")
    args = parser.parse_args()

    expected_v = args.current_ua * 1e-6 * LOAD_OHMS
    print("=" * 60)
    print("Intan pattern + Moku scope (intan_pattern_testing_guide.md §7)")
    print("=" * 60)
    print(f"Stim:     ch{args.ch}, {args.current_ua} µA  (~{expected_v:.2f} V on 10 kΩ)")
    print(f"Moku:     {args.moku}")
    print(f"Mode:     {args.mode}")
    print()

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        prep_stim(dev, args.ch, args.current_ua)

        scope_inp = args.scope_input
        if scope_inp == 0:
            print("Auto-detect scope input...")
            scope_inp = detect_scope_input(args.moku, dev, args.ch, args.current_ua)
        print(f"Scope:    Input{scope_inp}")
        print()

        if args.mode == "pulse100":
            status = load_single_pulse_pattern(dev, args.ch, args.current_ua, 100)
            expected = [100]
            t_before, t_after = -0.0005, 0.003
            cap_strategy = "poll"
            cap_repeat = 1
        elif args.mode == "burst":
            status = load_burst_pattern(dev, args.ch, args.current_ua, 100, 100, 10)
            expected = [100] * 10
            t_before, t_after = -0.0002, 0.006
            cap_strategy = "poll"
            cap_repeat = args.repeat
        elif args.mode == "sweep":
            durs = [500, 500, 200, 200, 100, 100, 50, 50, 20, 20, 10, 10]
            status = load_duration_sweep(dev, args.ch, args.current_ua, durs)
            expected = durs[0::2]
            t_before, t_after = -0.0005, 0.004
            cap_strategy = "poll"
            cap_repeat = 1
        else:
            status = load_single_pulse_pattern(dev, args.ch, args.current_ua, 500_000)
            expected = [500_000]
            t_before, t_after = -0.01, 0.06
            cap_strategy = "mid"
            cap_repeat = 1

        print("Pattern:", status)

        moku = MokuCapture(args.moku, scope_inp)
        try:
            trig = max(0.25, expected_v * 0.2)
            moku.configure(
                t_before_s=t_before,
                t_after_s=t_after,
                max_points=8192,
                trigger_level_v=trig,
                trigger_mode="Normal" if cap_strategy == "trigger" else "Auto",
            )
            t, v = run_synced_capture(
                moku, dev, cap_repeat, strategy=cap_strategy, mid_sample_s=0.35
            )
        finally:
            moku.close()

        if args.mode == "cal":
            v_max = float(np.max(v))
            ok = v_max > expected_v * 0.7
            print(f"Cal amplitude: {v_max:.3f} V (expect ~{expected_v:.2f} V) -> {'OK' if ok else 'FAIL'}")
        else:
            ok = analyze_pulses(t, v, expected)

        if args.mode == "cal" and ok:
            print("\n--- sweep (§10) ---")
            durs = [500, 500, 200, 200, 100, 100, 50, 50, 20, 20, 10, 10]
            status = load_duration_sweep(dev, args.ch, args.current_ua, durs)
            print("Pattern:", status)
            moku = MokuCapture(args.moku, scope_inp)
            try:
                moku.configure(
                    t_before_s=-0.01,
                    t_after_s=0.004,
                    max_points=8192,
                    trigger_level_v=trig,
                    trigger_mode="Normal",
                )
                t, v = run_synced_capture(moku, dev, 1, strategy="poll")
            finally:
                moku.close()
            ok = analyze_pulses(t, v, durs[0::2]) and ok

        for reg in (42, 40, 50):
            print(cmd(dev, f"READ {reg}"))

        if args.plot:
            try:
                import matplotlib.pyplot as plt

                fig, ax = plt.subplots(figsize=(12, 4))
                ax.plot(t * 1e6, v * 1e3, lw=0.8)
                ax.set_xlabel("time (µs)")
                ax.set_ylabel("mV")
                ax.set_title(f"ch{args.ch} stim @ Input{scope_inp}")
                ax.grid(True, alpha=0.3)
                fig.tight_layout()
                fig.savefig(args.plot, dpi=120)
                print(f"\nPlot: {args.plot}")
            except ImportError:
                pass

        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 3
    finally:
        close_device(dev, ifn)


if __name__ == "__main__":
    raise SystemExit(main())
