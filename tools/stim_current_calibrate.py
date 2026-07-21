#!/usr/bin/env python3
"""Калибровка stim-тока: Rigol V_avg, Reg34 step, sweep нескольких целевых µA."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_rigol_scope"))

from mcp_rigol_scope.lib import DHO804_RESOURCE, measure_plateau_v, measure_pulses_synced  # noqa: E402
from rigol_stim_smoke import load_pattern, usb_cmd  # noqa: E402
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402

LOAD_OHMS = 10_000.0
DEFAULT_TARGETS_UA = (50, 100, 180, 255)


@dataclass(frozen=True)
class StepPreset:
    name: str
    reg34: int
    reg35: int
    step_ua: float


# Intan RHS2116 step DAC (framework StimStepSize table)
STEP_PRESETS: dict[str, StepPreset] = {
    "500nA": StepPreset("500nA", 0x01E5, 0x0099, 0.5),
    "1uA": StepPreset("1uA", 0x00E2, 0x00AA, 1.0),
    "2uA": StepPreset("2uA", 0x005E, 0x00BB, 2.0),
    "5uA": StepPreset("5uA", 0x0026, 0x00EE, 5.0),
}


def magnitude_reg_val(magnitude_steps: int, trim: int = 128) -> int:
    mag = max(0, min(255, int(magnitude_steps)))
    tr = max(0, min(255, int(trim)))
    return (tr << 8) | mag


def prep_stim(dev, ch: int, step: StepPreset, magnitude_steps: int) -> None:
    reg_val = magnitude_reg_val(magnitude_steps)
    usb_cmd(dev, "INIT_STIM")
    usb_cmd(dev, f"WRITE 34 {step.reg34:#x} 0 0")
    usb_cmd(dev, f"WRITE 35 {step.reg35:#x} 0 0")
    usb_cmd(dev, "WRITE 42 0 1 0")
    usb_cmd(dev, "CLEAR_COMP")
    usb_cmd(dev, f"WRITE {64 + ch} {reg_val:#x} 0 0")
    usb_cmd(dev, f"WRITE {96 + ch} {reg_val:#x} 0 0")


def single_pulse_pattern(ch: int, reg_val: int, on_us: int) -> list[str]:
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


def measure_plateau(
    dev,
    resource: str,
    ch: int,
    step: StepPreset,
    magnitude_steps: int,
    *,
    on_us: int = 100_000,
    scope_channel: int = 1,
) -> tuple[float, float, int]:
    """Return (V_plateau, V_peak, n_plateau_samples)."""
    prep_stim(dev, ch, step, magnitude_steps)
    reg_val = magnitude_reg_val(magnitude_steps)
    load_pattern(dev, single_pulse_pattern(ch, reg_val, on_us))

    def run_pat() -> str:
        try:
            return usb_cmd(dev, "PATTERN_RUN 3", timeout_ms=30_000)
        finally:
            usb_cmd(dev, "WRITE 42 0 1 0", timeout_ms=5000)

    expected_v = magnitude_steps * step.step_ua * 1e-6 * LOAD_OHMS
    # timebase: pulse ~5 divisions wide for enough plateau samples
    time_scale_s = max(1e-3, on_us * 1e-6 / 5.0)
    res = measure_pulses_synced(
        resource,
        run_pat,
        channel=scope_channel,
        points=10000,
        scale_v=max(0.2, expected_v / 4.0),
        time_scale_s=time_scale_s,
        trigger_level_v=max(0.05, expected_v * 0.25),
        timeout_s=5.0,
        run_join_timeout_s=35.0,
        expected_plateau_v=expected_v,
        return_waveform=True,
    )
    if not res.get("ok"):
        raise RuntimeError(str(res))
    v = np.asarray(res["volts_v"], float)
    pl = measure_plateau_v(v, expected_v=expected_v)
    return float(pl["v_plateau_v"]), float(pl["v_peak_v"]), int(pl["n_plateau"])


def nominal_mag(target_ua: float, step_ua: float) -> int:
    return max(1, min(255, int(round(target_ua / step_ua))))


def cal_mag(target_ua: float, gain: float, step_ua: float) -> int:
    """gain = I_meas_uA / (mag * step_ua)."""
    if gain <= 0:
        raise ValueError("gain must be > 0")
    return max(1, min(255, int(round(target_ua / (gain * step_ua)))))


def pct_err(meas: float, expected: float) -> float:
    if expected == 0:
        return float("nan")
    return 100.0 * (meas - expected) / expected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=0)
    ap.add_argument("--rigol", default=DHO804_RESOURCE)
    ap.add_argument("--scope-channel", type=int, default=1)
    ap.add_argument("--load-ohms", type=float, default=LOAD_OHMS)
    ap.add_argument("--targets-ua", type=int, nargs="+", default=list(DEFAULT_TARGETS_UA))
    ap.add_argument(
        "--steps",
        nargs="+",
        default=["1uA"],
        choices=list(STEP_PRESETS.keys()),
        help="Reg34 step presets to test",
    )
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("-o", "--output", type=Path, default=Path("tools/testing/stim_cal_report.txt"))
    args = ap.parse_args()

    lines: list[str] = []
    log = lines.append

    log(f"Stim current calibration  load={args.load_ohms:.0f} Ω  ch={args.ch}")
    log(f"Rigol={args.rigol}  scope_ch={args.scope_channel}")
    log("")

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        stats = run_text_command(dev, "STATS", timeout_ms=5000, drain_before=True).strip()
        log(f"STATS: {stats}")
        r34 = run_text_command(dev, "READ 34", timeout_ms=5000, drain_before=True).strip()
        log(f"READ 34 before: {r34}")
        log("")

        for step_key in args.steps:
            step = STEP_PRESETS[step_key]
            log(f"=== Reg34 step {step.name}  R34={step.reg34:#06x}  R35={step.reg35:#06x}  step={step.step_ua} µA/LSB ===")
            raw_gains: list[float] = []

            log(f"{'target µA':>10} {'mag_nom':>8} {'V_plat V':>10} {'V_peak V':>10} {'I_meas µA':>10} {'err %':>8} {'n_plat':>6}")
            raw: dict[int, tuple[int, float, float]] = {}
            for target in args.targets_ua:
                mag = nominal_mag(float(target), step.step_ua)
                v_plat, v_peak, n_plat = measure_plateau(
                    dev,
                    args.rigol,
                    args.ch,
                    step,
                    mag,
                    scope_channel=args.scope_channel,
                )
                i_meas = v_plat / args.load_ohms * 1e6
                err = pct_err(i_meas, float(target))
                raw[target] = (mag, v_plat, i_meas)
                raw_gains.append(i_meas / (mag * step.step_ua))
                log(f"{target:10d} {mag:8d} {v_plat:10.4f} {v_peak:10.4f} {i_meas:10.1f} {err:+8.2f} {n_plat:6d}")

            gain = float(np.median(raw_gains))
            log(f"  → median gain factor (I_meas / mag / step_ua): {gain:.4f}")
            log("")
            log(f"{'target µA':>10} {'mag_cal':>8} {'V_plat V':>10} {'V_peak V':>10} {'I_meas µA':>10} {'err %':>8} {'n_plat':>6}")
            for target in args.targets_ua:
                mag_c = cal_mag(float(target), gain, step.step_ua)
                v_plat, v_peak, n_plat = measure_plateau(
                    dev,
                    args.rigol,
                    args.ch,
                    step,
                    mag_c,
                    scope_channel=args.scope_channel,
                )
                i_meas = v_plat / args.load_ohms * 1e6
                err = pct_err(i_meas, float(target))
                log(f"{target:10d} {mag_c:8d} {v_plat:10.4f} {v_peak:10.4f} {i_meas:10.1f} {err:+8.2f} {n_plat:6d}")
            log("")

        for reg in (42, 40):
            log(run_text_command(dev, f"READ {reg}", timeout_ms=5000, drain_before=True))
    finally:
        try:
            run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=False)
        except Exception:
            pass
        close_device(dev, ifn)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"Report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
