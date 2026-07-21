#!/usr/bin/env python3
"""Stim validation: STM32 PATTERN_* + Rigol scope (ch0, 10 kΩ load).

Runs wall-clock delay bench, amplitude cal, pulse-width sweep.
Writes report + optional CSV/plots under tools/testing/.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_rigol_scope"))

from pattern_sweep_lib import SWEEP_ON_US, build_sweep_usb_commands, current_reg_val  # noqa: E402
from test_pattern_timing import (  # noqa: E402
    PATTERN_DELAY_ONLY_1MS,
    bench,
    load_pattern,
)
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402

from lib import measure_pulses_synced  # noqa: E402

LOAD_OHMS = 10_000.0
RIGOL_RESOURCE = "USB0::6833::1101::DHO8A272405662::0::INSTR"


def usb_cmd(dev, text: str, *, timeout_ms: int = 120_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def single_pulse_pattern(ch: int, current_ua: int, on_us: int) -> list[str]:
    reg_val = current_reg_val(current_ua)
    mask = 1 << ch
    neg = (0x80 << 24) | ((64 + ch) << 16) | reg_val
    pos = (0x80 << 24) | ((96 + ch) << 16) | reg_val
    return [
        f"PATTERN_ADD_RAW {neg:#x}",
        f"PATTERN_ADD_RAW {pos:#x}",
        f"PATTERN_ADD_RAW {0x802C0000 | mask:#x}",
        f"PATTERN_ADD_RAW {0xA02A0000 | mask:#x}",
        f"PATTERN_ADD_DELAY_US {on_us}",
        "PATTERN_ADD_RAW 0xA02A0000",
    ]


def prep_stim(dev, ch: int, current_ua: int) -> None:
    reg_val = current_reg_val(current_ua)
    usb_cmd(dev, "INIT_STIM")
    usb_cmd(dev, "WRITE 42 0 1 0")
    usb_cmd(dev, "CLEAR_COMP")
    usb_cmd(dev, f"WRITE {64 + ch} {reg_val:#x} 0 0")
    usb_cmd(dev, f"WRITE {96 + ch} {reg_val:#x} 0 0")


def load_pattern_lines(dev, lines: list[str]) -> str:
    usb_cmd(dev, "PATTERN_CLEAR")
    for ln in lines:
        usb_cmd(dev, ln)
    return usb_cmd(dev, "PATTERN_STATUS")


@dataclass
class PulseStats:
    v_max: float
    v_median: float
    v_pp: float
    threshold_v: float
    widths_us: list[float]


def capture_during_pattern(
    resource: str,
    dev,
    *,
    channel: int,
    time_scale_s: float,
    time_offset_s: float,
    scale_v: float,
    trigger_level_v: float,
    points: int,
    repeat: int,
    run_timeout_ms: int,
    poll_wait_s: float = 0.15,
) -> tuple[np.ndarray, np.ndarray, PulseStats]:
    def run_pat() -> None:
        try:
            usb_cmd(dev, f"PATTERN_RUN {repeat}", timeout_ms=run_timeout_ms)
        finally:
            usb_cmd(dev, "WRITE 42 0 1 0", timeout_ms=5000)

    timeout_s = max(2.0, poll_wait_s + 2.0)
    res = measure_pulses_synced(
        resource,
        run_pat,
        channel=channel,
        points=points,
        scale_v=scale_v,
        offset_v=0.0,
        time_scale_s=time_scale_s,
        time_offset_s=time_offset_s,
        trigger_level_v=trigger_level_v,
        trigger_sweep="NORM",
        timeout_s=timeout_s,
        run_join_timeout_s=run_timeout_ms / 1000.0 + 10.0,
        return_waveform=True,
    )
    if not res.get("ok"):
        raise RuntimeError(str(res))
    t = res["time_s"]
    v = res["volts_v"]
    stats = PulseStats(
        v_max=float(res["v_max_v"]),
        v_median=float(res["v_median_v"]),
        v_pp=float(res["v_pp_v"]),
        threshold_v=float(res["threshold_v"]),
        widths_us=[float(x) for x in res["widths_us"]],
    )
    return t, v, stats


def analyze_widths(expected_us: list[int], widths_us: list[float], tolerance_pct: float) -> list[dict]:
    rows: list[dict] = []
    for i, meas in enumerate(widths_us[: len(expected_us)]):
        exp = expected_us[i]
        err = meas - exp
        err_pct = 100.0 * err / exp if exp else 0.0
        rows.append(
            {
                "idx": i + 1,
                "exp_us": exp,
                "meas_us": meas,
                "err_us": err,
                "err_pct": err_pct,
                "ok": abs(err_pct) <= tolerance_pct,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=0)
    ap.add_argument("--current-ua", type=int, default=180)
    ap.add_argument("--rigol", default=RIGOL_RESOURCE)
    ap.add_argument("--scope-channel", type=int, default=1)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--skip-timing", action="store_true")
    ap.add_argument("--skip-sweep", action="store_true")
    ap.add_argument("--tolerance-pct", type=float, default=15.0)
    ap.add_argument("-o", "--out-dir", default="tools/testing")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = out_dir / f"stim_rigol_ch{args.ch}_{stamp}_report.txt"
    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    expected_v = args.current_ua * 1e-6 * LOAD_OHMS
    log(f"Stim Rigol validation  UTC={stamp}")
    log(f"ch={args.ch}  current={args.current_ua} µA  load={LOAD_OHMS/1000:.0f} kΩ  expected V={expected_v:.3f}")
    log(f"rigol={args.rigol}  scope_ch={args.scope_channel}")
    log()

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        stats = run_text_command(dev, "STATS", timeout_ms=5000, drain_before=True).strip()
        log(f"STATS: {stats}")
        log()

        prep_stim(dev, args.ch, args.current_ua)

        if not args.skip_timing:
            log("=== Wall-clock DWT timing (channel-independent) ===")
            load_pattern(dev, PATTERN_DELAY_ONLY_1MS)
            bench(dev, "1000 µs delay only", PATTERN_DELAY_ONLY_1MS, repeats=[50, 200], delay_us_per_iter=1000)
            ch_pat = single_pulse_pattern(args.ch, args.current_ua, 100)
            load_pattern(dev, ch_pat)
            bench(
                dev,
                f"ch{args.ch} single 100 µs pulse",
                ch_pat,
                repeats=[100, 500],
                delay_us_per_iter=100,
            )
            log()

        log("=== Rigol amplitude cal (500 ms ON) ===")
        cal_pat = single_pulse_pattern(args.ch, args.current_ua, 500_000)
        load_pattern_lines(dev, cal_pat)
        _, _, cal = capture_during_pattern(
            args.rigol,
            dev,
            channel=args.scope_channel,
            time_scale_s=0.05,
            time_offset_s=0.0,
            scale_v=0.5,
            trigger_level_v=max(0.2, expected_v * 0.3),
            points=4000,
            repeat=1,
            run_timeout_ms=900_000,
            poll_wait_s=0.4,
        )
        i_meas = cal.v_max / LOAD_OHMS
        i_err_pct = 100.0 * (i_meas - args.current_ua * 1e-6) / (args.current_ua * 1e-6)
        log(f"V_max={cal.v_max:.4f} V  V_pp={cal.v_pp:.4f} V  I_est={i_meas*1e6:.1f} µA  err={i_err_pct:+.1f}%")
        log(f"PASS amplitude" if cal.v_max > expected_v * 0.7 else "FAIL amplitude (<70% expected)")
        log()

        log("=== Rigol pulse 100 µs ===")
        pat100 = single_pulse_pattern(args.ch, args.current_ua, 100)
        load_pattern_lines(dev, pat100)
        _, _, p100 = capture_during_pattern(
            args.rigol,
            dev,
            channel=args.scope_channel,
            time_scale_s=2e-4,
            time_offset_s=0.0,
            scale_v=0.2,
            trigger_level_v=max(0.1, expected_v * 0.25),
            points=8000,
            repeat=10,
            run_timeout_ms=60_000,
        )
        w100 = p100.widths_us[0] if p100.widths_us else float("nan")
        if p100.widths_us:
            err100 = 100.0 * (w100 - 100) / 100
            log(f"width={w100:.1f} µs  err={err100:+.1f}%  pulses={len(p100.widths_us)}")
        else:
            log("ERR no pulses detected for 100 µs")
        log()

        if not args.skip_sweep:
            log("=== Rigol duration sweep ===")
            sweep_cmds = build_sweep_usb_commands(args.ch, args.current_ua, SWEEP_ON_US, gap_ms=300)[:-1]
            for text in sweep_cmds:
                usb_cmd(dev, text)
            log(usb_cmd(dev, "PATTERN_STATUS"))
            _, _, sw = capture_during_pattern(
                args.rigol,
                dev,
                channel=args.scope_channel,
                time_scale_s=5e-4,
                time_offset_s=0.0,
                scale_v=0.5,
                trigger_level_v=max(0.1, expected_v * 0.2),
                points=8000,
                repeat=3,
                run_timeout_ms=120_000,
                poll_wait_s=0.5,
            )
            on_expected = SWEEP_ON_US
            rows = analyze_widths(on_expected, sw.widths_us[: len(on_expected)], args.tolerance_pct)
            log(f"{'#':>3} {'exp_us':>7} {'meas_us':>9} {'err%':>7} {'ok':>4}")
            for r in rows:
                log(f"{r['idx']:3d} {r['exp_us']:7d} {r['meas_us']:9.1f} {r['err_pct']:+6.1f}% {'OK' if r['ok'] else 'FAIL'}")
            if len(sw.widths_us) > len(on_expected):
                log(f"... +{len(sw.widths_us) - len(on_expected)} extra pulses in capture")
            log()

        for reg in (42, 40, 50):
            log(usb_cmd(dev, f"READ {reg}", timeout_ms=5000))

    finally:
        try:
            run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=False)
        except Exception:
            pass
        close_device(dev, ifn)

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"\nReport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
