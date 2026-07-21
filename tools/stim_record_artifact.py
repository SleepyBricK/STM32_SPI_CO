#!/usr/bin/env python3
"""Оценка артефакта записи (RR8 ch0) вокруг stim-паттерна на том же канале.

Прошивка не поддерживает одновременный SPI_STREAM_FW + PATTERN_RUN — измеряем:
  1) baseline RR8 до stim;
  2) RR8 сразу после PATTERN_RUN (остаточный/зарядовый артефакт);
  3) теоретический in-pulse артефакт по V_stim и общему elec0.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_rigol_scope"))

from ch_fw_channel_scan import ch_stats, parse_clip, prep  # noqa: E402
from ch_fw_long_suite import capture_fw16, skip_warmup, uv  # noqa: E402
from fw_constants import FW_KSPS_DEFAULT  # noqa: E402
from pattern_sweep_lib import current_reg_val  # noqa: E402
from rigol_stim_smoke import load_pattern, prep_ch0_stim, usb_cmd  # noqa: E402
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402

ADC_MID = 32768.0
UV_PER_LSB = 0.195
R_STIM_OHMS = 10_000.0


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


@dataclass
class WindowStats:
    label: str
    n: int
    med_uv: float
    rms_uv: float
    ptp_uv: float
    peak_uv: float
    clip: int


def rr8_window(dev, n_per_ch: int, ksps: int, warmup_s: float, label: str) -> WindowStats:
    prep(dev)
    arrs, stats = capture_fw16(dev, n_per_ch, ksps)
    codes = arrs[0]
    fs = ksps * 1000.0
    trimmed = skip_warmup(codes, fs, warmup_s)
    u = uv(trimmed)
    return WindowStats(
        label=label,
        n=len(trimmed),
        med_uv=float(np.median(u)),
        rms_uv=float(np.sqrt(np.mean(u**2))),
        ptp_uv=float(np.max(u) - np.min(u)),
        peak_uv=float(np.max(np.abs(u))),
        clip=parse_clip(stats),
    )


def theoretical_in_pulse_uv(current_ua: int, r_stim: float, r_rec: float) -> dict[str, float]:
    """elec0: stim branch R_stim and recording shunt R_rec both to GND (упрощённая модель)."""
    i_a = current_ua * 1e-6
    v_elec = i_a * r_stim  # ток стима через нагрузку stim
    i_shunt = v_elec / r_rec  # ток через 1kΩ shunt
    v_adc_artifact = v_elec  # тот же потенциал elec0 на ADC
    return {
        "v_stim_load_v": v_elec,
        "v_adc_artifact_v": v_adc_artifact,
        "v_adc_artifact_uv": v_adc_artifact * 1e6,
        "i_shunt_ua": i_shunt * 1e6,
        "i_stim_ua": current_ua,
        "codes_delta": v_adc_artifact * 1e6 / UV_PER_LSB,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=0)
    ap.add_argument("--current-ua", type=int, default=180)
    ap.add_argument("--ksps", type=int, default=FW_KSPS_DEFAULT)
    ap.add_argument("--baseline-s", type=float, default=10.0)
    ap.add_argument("--post-s", type=float, default=5.0)
    ap.add_argument("--on-ms", type=float, default=100.0)
    ap.add_argument("--pattern-repeat", type=int, default=20)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--warmup-skip", type=float, default=0.5)
    ap.add_argument("--r-rec-ohms", type=float, default=1000.0)
    ap.add_argument("--r-stim-ohms", type=float, default=R_STIM_OHMS)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("-o", type=Path, default=Path("tools/testing/stim_record_artifact_report.txt"))
    args = ap.parse_args()

    n_base = int(args.baseline_s * args.ksps * 1000)
    n_post = int(args.post_s * args.ksps * 1000)
    on_us = int(args.on_ms * 1000)

    lines: list[str] = []
    log = lines.append
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log(f"Stim recording artifact assessment  UTC={ts}")
    log(f"ch={args.ch}  I_stim={args.current_ua} µA  R_rec={args.r_rec_ohms:.0f}Ω  R_stim={args.r_stim_ohms:.0f}Ω")
    log(f"RR8 {args.ksps} kS/s/ch  baseline={args.baseline_s}s  post={args.post_s}s  pattern=ON {args.on_ms}ms x{args.pattern_repeat}")
    log("")

    theory = theoretical_in_pulse_uv(args.current_ua, args.r_stim_ohms, args.r_rec_ohms)
    log("=== Теория (in-pulse, общий elec0) ===")
    log(f"  V на stim-нагрузке ≈ {theory['v_stim_load_v']:.3f} V")
    log(f"  Артефакт на ADC (elec0) ≈ {theory['v_adc_artifact_uv']:.0f} µV  (~{theory['codes_delta']:.0f} LSB от mid)")
    log(f"  Ток через shunt {args.r_rec_ohms:.0f}Ω ≈ {theory['i_shunt_ua']:.0f} µA")
    log("  Примечание: SPI_STREAM_FW и PATTERN_RUN одновременно недоступны — in-pulse не снимаем live.")
    log("")

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        stats0 = run_text_command(dev, "STATS", timeout_ms=5000, drain_before=True).strip()
        log(f"STATS: {stats0}")
        log("")

        base = rr8_window(dev, n_base, args.ksps, args.warmup_skip, "baseline")
        log("=== Baseline RR8 (stim OFF) ===")
        log(f"  ch0 n={base.n}  med={base.med_uv:+.1f} µV  RMS={base.rms_uv:.1f} µV  ptp={base.ptp_uv:.1f} µV  peak|x|={base.peak_uv:.1f} µV  clip={base.clip}")

        prep_ch0_stim(dev, args.current_ua)
        load_pattern(dev, single_pulse_pattern(args.ch, args.current_ua, on_us))

        post_rows: list[WindowStats] = []
        for i in range(args.cycles):
            t0 = time.perf_counter()
            usb_cmd(dev, f"PATTERN_RUN {args.pattern_repeat}", timeout_ms=120_000)
            usb_cmd(dev, "WRITE 42 0 1 0", timeout_ms=5000)
            pat_s = time.perf_counter() - t0
            post = rr8_window(dev, n_post, args.ksps, args.warmup_skip, f"post_cycle_{i+1}")
            post_rows.append(post)
            log("")
            log(f"=== Cycle {i+1}/{args.cycles}  pattern_wall={pat_s:.2f}s ===")
            log(f"  post-stim RR8 ch0: med={post.med_uv:+.1f} µV  RMS={post.rms_uv:.1f} µV  ptp={post.ptp_uv:.1f} µV  peak|x|={post.peak_uv:.1f} µV  clip={post.clip}")
            log(f"  ΔRMS vs baseline: {post.rms_uv - base.rms_uv:+.1f} µV  ({100*(post.rms_uv/base.rms_uv-1):+.1f}%)")
            log(f"  Δmed vs baseline: {post.med_uv - base.med_uv:+.1f} µV")

        if post_rows:
            rms_mean = float(np.mean([r.rms_uv for r in post_rows]))
            med_mean = float(np.mean([r.med_uv for r in post_rows]))
            log("")
            log("=== Сводка post-stim (среднее по циклам) ===")
            log(f"  baseline RMS={base.rms_uv:.1f} µV  med={base.med_uv:+.1f} µV")
            log(f"  post-stim RMS={rms_mean:.1f} µV  med={med_mean:+.1f} µV")
            log(f"  остаточный артефакт RMS: {rms_mean - base.rms_uv:+.1f} µV")
            log(f"  остаточный артефакт med: {med_mean - base.med_uv:+.1f} µV")
            if theory["v_adc_artifact_uv"] > 0:
                residual_pct = 100.0 * (rms_mean - base.rms_uv) / theory["v_adc_artifact_uv"]
                log(f"  post-stim / in-pulse theory: {residual_pct:.2f}% (RMS)")
        log("")
        log("=== In-pulse (оценка, не измерено live) ===")
        log(f"  ожидаемый уровень ch0 во время ON: ~{theory['v_adc_artifact_uv']:.0f} µV ({theory['v_adc_artifact_uv']/1e6:.3f} V)")
        log(f"  vs baseline RMS {base.rms_uv:.1f} µV → усиление ~{theory['v_adc_artifact_uv']/max(base.rms_uv,1):.0f}×")

        log("")
        for reg in (42, 40):
            log(run_text_command(dev, f"READ {reg}", timeout_ms=5000, drain_before=True))
    finally:
        try:
            run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=False)
        except Exception:
            pass
        close_device(dev, ifn)

    args.o.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    args.o.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"Report: {args.o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
