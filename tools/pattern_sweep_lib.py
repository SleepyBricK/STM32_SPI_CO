"""Sweep стим-импульсов: ch2, 180 µA, PATTERN_ADD_WRITE для R42/R44 (3-slot pipeline)."""

from __future__ import annotations

SWEEP_ON_US = [500, 200, 100, 50, 20, 10]
DEFAULT_CH = 2
DEFAULT_CURRENT_UA = 180
DEFAULT_REPEAT = 20
DEFAULT_GAP_MS = 300  # пауза между полными sweep-циклами внутри PATTERN_RUN


def current_reg_val(current_ua: int) -> int:
    if not 0 <= current_ua <= 255:
        raise ValueError(f"current_ua must be 0..255 (µA in reg LSB), got {current_ua}")
    return 0x8000 | (current_ua & 0xFF)


def build_sweep_usb_commands(
    ch: int = DEFAULT_CH,
    current_ua: int = DEFAULT_CURRENT_UA,
    on_us_list: list[int] | None = None,
    gap_ms: int = DEFAULT_GAP_MS,
) -> list[str]:
    """Полная последовательность USB-команд: init + load pattern (без PATTERN_RUN)."""
    if on_us_list is None:
        on_us_list = SWEEP_ON_US
    reg_val = current_reg_val(current_ua)
    mask = 1 << ch
    neg = (0x80 << 24) | ((64 + ch) << 16) | reg_val
    pos = (0x80 << 24) | ((96 + ch) << 16) | reg_val

    lines = [
        "INIT_STIM",
        "WRITE 42 0 1 0",
        "CLEAR_COMP",
        f"WRITE {64 + ch} {reg_val:#x} 0 0",
        f"WRITE {96 + ch} {reg_val:#x} 0 0",
        "PATTERN_CLEAR",
        f"PATTERN_ADD_RAW {neg:#x}",
        f"PATTERN_ADD_RAW {pos:#x}",
        f"PATTERN_ADD_WRITE 44 {mask} 0 0",
    ]
    for us in on_us_list:
        lines.append(f"PATTERN_ADD_WRITE 42 {mask} 1 0")
        lines.append(f"PATTERN_ADD_DELAY_US {us}")
        lines.append("PATTERN_ADD_WRITE 42 0 1 0")
        lines.append(f"PATTERN_ADD_DELAY_US {us}")
    if gap_ms > 0:
        lines.append(f"PATTERN_ADD_DELAY_US {gap_ms * 1000}")
    lines.append("PATTERN_STATUS")
    return lines


def prep_and_load_sweep(
    dev,
    ch: int,
    current_ua: int,
    on_us_list: list[int],
    cmd_fn,
    *,
    gap_ms: int = DEFAULT_GAP_MS,
) -> str:
    """cmd_fn(dev, text) -> reply; последняя команда — PATTERN_STATUS."""
    for text in build_sweep_usb_commands(ch, current_ua, on_us_list, gap_ms=gap_ms)[:-1]:
        reply = cmd_fn(dev, text)
        if reply.startswith("ERR"):
            raise RuntimeError(f"{text!r} -> {reply}")
    return cmd_fn(dev, "PATTERN_STATUS")
