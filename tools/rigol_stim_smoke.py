#!/usr/bin/env python3
"""Smoke test: Rigol DHO804 + STM32 Intan stim pattern, no manual steps."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_rigol_scope"))

from mcp_rigol_scope.lib import DHO804_RESOURCE, find_rigol_resource, measure_pulses_synced, open_instrument  # noqa: E402
from pattern_sweep_lib import current_reg_val  # noqa: E402
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402

LOAD_OHMS = 10_000.0


def usb_cmd(dev, text: str, *, timeout_ms: int = 30_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def ch0_single_pulse_pattern(current_ua: int, on_us: int) -> list[str]:
    reg_val = current_reg_val(current_ua)
    mask = 1
    neg = (0x80 << 24) | (64 << 16) | reg_val
    pos = (0x80 << 24) | (96 << 16) | reg_val
    return [
        f"PATTERN_ADD_RAW {neg:#x}",
        f"PATTERN_ADD_RAW {pos:#x}",
        f"PATTERN_ADD_RAW {0x802C0000 | mask:#x}",
        f"PATTERN_ADD_RAW {0xA02A0000 | mask:#x}",
        f"PATTERN_ADD_DELAY_US {on_us}",
        "PATTERN_ADD_RAW 0xA02A0000",
    ]


def prep_ch0_stim(dev, current_ua: int) -> None:
    reg_val = current_reg_val(current_ua)
    usb_cmd(dev, "INIT_STIM")
    usb_cmd(dev, "WRITE 42 0 1 0")
    usb_cmd(dev, "CLEAR_COMP")
    usb_cmd(dev, f"WRITE 64 {reg_val:#x} 0 0")
    usb_cmd(dev, f"WRITE 96 {reg_val:#x} 0 0")


def load_pattern(dev, lines: list[str]) -> str:
    usb_cmd(dev, "PATTERN_CLEAR")
    for line in lines:
        usb_cmd(dev, line)
    return usb_cmd(dev, "PATTERN_STATUS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rigol", default=DHO804_RESOURCE)
    ap.add_argument("--scope-channel", type=int, default=1)
    ap.add_argument("--current-ua", type=int, default=180)
    ap.add_argument("--on-ms", type=float, default=100.0)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--min-vmax", type=float, default=0.5)
    ap.add_argument("--no-reset", action="store_true")
    args = ap.parse_args()

    resource = find_rigol_resource(args.rigol or None)
    if not resource:
        print("FAIL: Rigol не найден по USB/VISA")
        return 2

    try:
        with open_instrument(resource, timeout_ms=5000) as scope:
            ident = scope.idn()
            print(f"Rigol: {ident.manufacturer} {ident.model} serial={ident.serial} resource={resource}")
    except Exception as exc:
        print(f"FAIL: cannot open Rigol {resource}: {exc}")
        return 2

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        stats = run_text_command(dev, "STATS", timeout_ms=5000, drain_before=True).strip()
        print(f"STM32: {stats}")

        prep_ch0_stim(dev, args.current_ua)
        on_us = int(round(args.on_ms * 1000.0))
        print(load_pattern(dev, ch0_single_pulse_pattern(args.current_ua, on_us)))

        def run_pattern() -> str:
            try:
                return usb_cmd(dev, f"PATTERN_RUN {args.repeat}", timeout_ms=30_000)
            finally:
                usb_cmd(dev, "WRITE 42 0 1 0", timeout_ms=5000)

        expected_v = args.current_ua * 1e-6 * LOAD_OHMS
        result = measure_pulses_synced(
            resource,
            run_pattern,
            channel=args.scope_channel,
            points=10_000,
            scale_v=0.5,
            time_scale_s=0.05,
            time_offset_s=0.0,
            trigger_level_v=max(0.2, expected_v * 0.25),
            timeout_s=3.0,
            run_join_timeout_s=35.0,
        )
        if not result.get("ok"):
            print(f"FAIL: {result}")
            return 1

        v_max = float(result["v_max_v"])
        v_pp = float(result["v_pp_v"])
        widths = [float(w) for w in result["widths_us"]]
        print(f"Rigol capture: V_max={v_max:.3f} V  V_pp={v_pp:.3f} V  pulses={len(widths)}")
        if widths:
            print(f"First width: {widths[0]:.1f} us")
        if v_max <= args.min_vmax:
            print(f"FAIL: V_max <= {args.min_vmax:.3f} V")
            return 1
        print("PASS")
        return 0
    finally:
        try:
            run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=False)
        except Exception:
            pass
        close_device(dev, ifn)


if __name__ == "__main__":
    raise SystemExit(main())
