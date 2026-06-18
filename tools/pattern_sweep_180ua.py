#!/usr/bin/env python3
"""
Sweep длительностей стим-импульсов: ch2, 180 µA, R42 ON/OFF через PATTERN_ADD_WRITE (U=1).

Один PATTERN_RUN = 6 импульсов с ON/OFF delay: 500, 200, 100, 50, 20, 10 µs (OFF delay = ON).

Примеры:
  python3 tools/pattern_sweep_180ua.py --print-bash
  python3 tools/pattern_sweep_180ua.py --no-reset --run 20
  python3 tools/pattern_sweep_180ua.py --no-reset --run 20 --gap-ms 500
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pattern_sweep_lib import (  # noqa: E402
    DEFAULT_CH,
    DEFAULT_CURRENT_UA,
    DEFAULT_GAP_MS,
    DEFAULT_REPEAT,
    SWEEP_ON_US,
    build_sweep_usb_commands,
    current_reg_val,
    prep_and_load_sweep,
)
from usb_intan_lib import close_device, open_device, run_text_command  # noqa: E402


def cmd(dev, text: str, *, timeout_ms: int = 120_000) -> str:
    reply = run_text_command(dev, text, timeout_ms=timeout_ms, drain_before=True)
    if reply.startswith("ERR"):
        raise RuntimeError(f"{text!r} -> {reply}")
    return reply


def load_smoke_pattern(dev, ch: int, current_ua: int, on_ms: int, cmd_fn) -> str:
    """Один длинный импульс — удобно увидеть на Moku live (Input1, DC, ~1.6 V)."""
    reg_val = current_reg_val(current_ua)
    mask = 1 << ch
    neg = (0x80 << 24) | ((64 + ch) << 16) | reg_val
    pos = (0x80 << 24) | ((96 + ch) << 16) | reg_val
    on_us = on_ms * 1000
    for text in (
        "INIT_STIM",
        "WRITE 42 0 1 0",
        "CLEAR_COMP",
        f"WRITE {64 + ch} {reg_val:#x} 0 0",
        f"WRITE {96 + ch} {reg_val:#x} 0 0",
        "PATTERN_CLEAR",
        f"PATTERN_ADD_RAW {neg:#x}",
        f"PATTERN_ADD_RAW {pos:#x}",
        f"PATTERN_ADD_WRITE 44 {mask} 0 0",
        f"PATTERN_ADD_WRITE 42 {mask} 1 0",
        f"PATTERN_ADD_DELAY_US {on_us}",
        "PATTERN_ADD_WRITE 42 0 1 0",
    ):
        reply = cmd_fn(dev, text)
        if reply.startswith("ERR"):
            raise RuntimeError(f"{text!r} -> {reply}")
    return cmd_fn(dev, "PATTERN_STATUS")


def print_bash(
    ch: int, current_ua: int, on_us_list: list[int], repeat: int, gap_ms: int
) -> None:
    print(
        "# ВНИМАНИЕ: это только печать команд. На STM32 ничего не уходит.",
        file=sys.stderr,
    )
    print(
        f"# Запуск: python3 tools/pattern_sweep_180ua.py --no-reset --run {repeat} --gap-ms {gap_ms}",
        file=sys.stderr,
    )
    print(
        "# Проверка осцилла: python3 tools/pattern_sweep_180ua.py --no-reset --smoke",
        file=sys.stderr,
    )
    print("#!/bin/bash")
    print("# ch{} {} µA, sweep ON µs: {}".format(ch, current_ua, on_us_list))
    print("set -euo pipefail")
    print('CMD="python3 tools/usb_intan_cmd.py --no-reset"')
    for line in build_sweep_usb_commands(ch, current_ua, on_us_list, gap_ms=gap_ms)[:-1]:
        print(f'$CMD "{line}"')
    print('$CMD PATTERN_STATUS')
    print(f'$CMD "PATTERN_RUN {repeat}" --timeout-ms 60000')
    print('$CMD "WRITE 42 0 1 0"')


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep стим ch2 180 µA (WRITE R42/R44)")
    ap.add_argument("--ch", type=int, default=DEFAULT_CH)
    ap.add_argument("--current-ua", type=int, default=DEFAULT_CURRENT_UA)
    ap.add_argument("--on-us", type=int, nargs="+", default=None, help=f"default: {SWEEP_ON_US}")
    ap.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help=f"сколько раз повторить весь sweep в одном PATTERN_RUN (default {DEFAULT_REPEAT})",
    )
    ap.add_argument("--run", type=int, default=None, metavar="N", help="alias для --repeat")
    ap.add_argument(
        "--gap-ms",
        type=int,
        default=DEFAULT_GAP_MS,
        help=f"пауза OFF между sweep-циклами, ms (default {DEFAULT_GAP_MS})",
    )
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--print-bash", action="store_true", help="только вывести bash, USB не трогать")
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="один импульс 500 ms ON (Moku Input1, DC, timebase ~100 ms/div)",
    )
    ap.add_argument("--smoke-ms", type=int, default=500)
    ap.add_argument("--with-scope", action="store_true", help="Moku capture через pattern_sweep_scope.py")
    ap.add_argument("--moku", default="mokugo-002464.local")
    ap.add_argument("--scope-input", type=int, default=1)
    args = ap.parse_args()

    on_us = args.on_us if args.on_us is not None else list(SWEEP_ON_US)
    repeat = args.run if args.run is not None else args.repeat

    if args.print_bash:
        print_bash(args.ch, args.current_ua, on_us, repeat, args.gap_ms)
        return 0

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        if args.smoke:
            print(
                f"SMOKE: ch{args.ch} {args.current_ua} µA, ON {args.smoke_ms} ms — "
                f"Moku Input1, DC, ожидай ~{args.current_ua * 10_000 / 1e6:.2f} V на 10 kΩ"
            )
            status = load_smoke_pattern(dev, args.ch, args.current_ua, args.smoke_ms, cmd)
            print("Loaded:", status)
            reply = cmd(dev, "PATTERN_RUN 1", timeout_ms=max(120_000, args.smoke_ms * 3))
            print(reply)
            cmd(dev, "WRITE 42 0 1 0")
            print("OK smoke done")
            return 0

        if args.with_scope:
            close_device(dev, ifn)
            dev = ifn = None
    finally:
        if dev is not None:
            close_device(dev, ifn)

    if args.with_scope:
        scope = ROOT / "tools" / "pattern_sweep_scope.py"
        scope_argv = [
            sys.executable,
            str(scope),
            f"--ch={args.ch}",
            f"--current-ua={args.current_ua}",
            f"--repeat={repeat}",
            f"--gap-ms={args.gap_ms}",
            f"--moku={args.moku}",
            f"--scope-input={args.scope_input}",
        ]
        if args.no_reset:
            scope_argv.append("--no-reset")
        return subprocess.call(scope_argv)

    cycle_ms = args.gap_ms + 15  # ~15 ms SPI+sweep на цикл
    total_ms = repeat * cycle_ms
    print(f"ch{args.ch}, {args.current_ua} µA, sweep ON (µs): {on_us}, OFF delay = ON")
    print(f"Один sweep = {len(on_us)} импульсов; PATTERN_RUN ×{repeat}, gap {args.gap_ms} ms между sweep")
    print(f"Ожидаемая длительность ~{total_ms / 1000:.1f} s")
    print("Moku: Input1, Auto, 0.3 V, timebase span ~{:.0f} ms или больше".format(max(500, total_ms)))
    print()

    dev, ifn = open_device(reset=not args.no_reset)
    try:
        status = prep_and_load_sweep(
            dev, args.ch, args.current_ua, on_us, cmd, gap_ms=args.gap_ms
        )
        print("Loaded:", status)
        timeout_ms = max(120_000, int(total_ms * 2))
        wall_reply = cmd(dev, f"PATTERN_RUN {repeat}", timeout_ms=timeout_ms)
        print(wall_reply)
        cmd(dev, "WRITE 42 0 1 0")
    finally:
        close_device(dev, ifn)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
