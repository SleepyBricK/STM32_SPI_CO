#!/usr/bin/env python3
"""Пакетный замер импеданса RHS2116 (аналог ioctl measure_impedance_raw для USB)."""

from __future__ import annotations

import math
import re
import time
from typing import Callable, List, Sequence, Tuple

INTAN_SINE64 = (
    128, 140, 152, 164, 176, 187, 198, 209, 218, 227, 235, 242, 248, 253, 255, 255,
    255, 255, 253, 248, 242, 235, 227, 218, 209, 198, 187, 176, 164, 152, 140, 128,
    116, 104, 92, 80, 69, 58, 47, 38, 29, 21, 14, 8, 3, 1, 1, 1,
    1, 1, 3, 8, 14, 21, 29, 38, 47, 58, 69, 80, 92, 104, 116, 128,
)

IMPEDANCE_MAX_AVERAGES = 1000
IMPEDANCE_MAX_SAMPLES = 2048
IMPEDANCE_SAMPLE_RATE_EST_HZ = 11500
IMPEDANCE_MIN_PERIODS = 8
IMPEDANCE_MIN_SPP = 6

RHS2116_REG2_ZCHECK_OFF = 0x0000
RHS2116_REG3_ZCHECK_NEUTRAL = 0x0080


def select_impedance_profile(
    frequency_hz: int, requested_samples: int
) -> tuple[int, int]:
    """Возвращает (samples_per_period, num_periods)."""
    min_periods = IMPEDANCE_MIN_PERIODS
    if frequency_hz == 100:
        spp, min_periods = 8, 5
    elif frequency_hz == 300:
        spp, min_periods = 32, 5
    elif frequency_hz == 1000:
        spp, min_periods = 16, 8
    elif frequency_hz == 2000:
        spp, min_periods = 5, 10
    else:
        spp = max(
            IMPEDANCE_MIN_SPP,
            (IMPEDANCE_SAMPLE_RATE_EST_HZ + frequency_hz // 2) // frequency_hz,
        )

    spp = min(spp, IMPEDANCE_MAX_SAMPLES)
    num_periods = max(min_periods, (requested_samples + spp - 1) // spp)
    return spp, num_periods


def _parse_reply_int(reply: str, key: str, default: int = 0) -> int:
    match = re.search(rf"\b{re.escape(key)}=(-?\d+)", reply)
    if not match:
        return default
    return int(match.group(1))


def parse_impedance_measure_reply(reply: str) -> dict:
    """
    Парсит ответ STM32: OK IMPEDANCE channel=… sin_accum=… cos_accum=…
    (см. usb_stream_service.c, intan_impedance_guide.md).
    """
    text = (reply or "").strip()
    if text.startswith("ERR"):
        raise ValueError(text)
    if "IMPEDANCE" not in text:
        raise ValueError(f"unexpected impedance reply: {text!r}")

    sin_accum = _parse_reply_int(text, "sin_accum")
    cos_accum = _parse_reply_int(text, "cos_accum")
    p0_sin = _parse_reply_int(text, "p0_sin", sin_accum)
    p0_cos = _parse_reply_int(text, "p0_cos", cos_accum)
    sample_count = _parse_reply_int(text, "sample_count")
    actual_freq_millihz = _parse_reply_int(text, "actual_freq_millihz")

    return {
        "channel": _parse_reply_int(text, "channel"),
        "scale_bits": _parse_reply_int(text, "scale"),
        "freq_hz": _parse_reply_int(text, "freq_hz"),
        "actual_freq_millihz": actual_freq_millihz,
        "samples_per_period": _parse_reply_int(text, "samples_per_period"),
        "periods": _parse_reply_int(text, "periods"),
        "sample_count": sample_count,
        "actual_num_samples": sample_count,
        "effective_frequency_hz": actual_freq_millihz / 1000.0,
        "sin_accum": sin_accum,
        "cos_accum": cos_accum,
        "overruns": _parse_reply_int(text, "overruns"),
        "spi_errors": _parse_reply_int(text, "spi_errors"),
        "clipped": _parse_reply_int(text, "clipped"),
        "points": [{"sin_accum": p0_sin, "cos_accum": p0_cos}],
    }


def rhs2116_safe_impedance_commands() -> List[Tuple]:
    return [
        ("write", 2, RHS2116_REG2_ZCHECK_OFF, 0, 0),
        ("sleep", 0.001),
        ("write", 3, RHS2116_REG3_ZCHECK_NEUTRAL, 0, 0),
        ("sleep", 0.001),
        ("write", 44, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 46, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 48, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 42, 0x0000, 1, 0),
        ("sleep", 0.001),
        ("clear_compliance",),
        ("sleep", 0.001),
    ]


def run_rhs2116_sequence(
    spi,
    commands: Sequence[Tuple],
    *,
    read_register: Callable,
    write_register: Callable,
    clear_adc: Callable,
    clear_compliance: Callable,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    for command in commands:
        op = command[0]
        if op == "read":
            read_register(spi, int(command[1]), verbose=False)
        elif op == "write":
            _, reg, value, u_flag, m_flag = command
            write_register(
                spi,
                int(reg),
                int(value),
                u_flag=int(u_flag),
                m_flag=int(m_flag),
                verbose=False,
            )
        elif op == "clear_adc":
            clear_adc(spi, verbose=False)
        elif op == "clear_compliance":
            clear_compliance(spi, verbose=False)
        elif op == "sleep":
            sleep_fn(float(command[1]))


def _wait_until_ns(deadline_ns: float) -> None:
    while True:
        now_ns = time.monotonic_ns()
        if now_ns >= deadline_ns:
            break
        remaining_ns = deadline_ns - now_ns
        if remaining_ns >= 100_000:
            time.sleep(remaining_ns / 1e9 * 0.9)
        elif remaining_ns > 5_000:
            time.sleep(remaining_ns / 1e9)
        else:
            time.sleep(0)


def measure_impedance_raw(
    spi,
    channel: int,
    scale_bits: int,
    *,
    num_samples: int = 64,
    frequency_hz: int = 1000,
    num_averages: int = 1,
    write_register: Callable,
    convert: Callable[[int], int],
    clear_adc: Callable,
    stop_stream: Callable | None = None,
) -> dict:
    """
    Пакетный замер импеданса через цикл WRITE Reg3 + CONVERT.

    Возвращает dict, совместимый с intan_driver.measure_impedance_raw().
    """
    if not (0 <= channel <= 15):
        raise ValueError("channel must be 0-15")
    if scale_bits not in (0, 1, 3):
        raise ValueError("scale_bits must be 0, 1, or 3")
    if not (1 <= num_averages <= IMPEDANCE_MAX_AVERAGES):
        raise ValueError(f"num_averages must be 1..{IMPEDANCE_MAX_AVERAGES}")
    if not (1 <= num_samples <= IMPEDANCE_MAX_SAMPLES):
        raise ValueError(f"num_samples must be 1..{IMPEDANCE_MAX_SAMPLES}")
    if frequency_hz <= 0:
        raise ValueError("frequency_hz must be > 0")

    if stop_stream is not None:
        try:
            stop_stream()
        except Exception:
            pass

    samples_per_period, num_periods = select_impedance_profile(
        int(frequency_hz), int(num_samples)
    )
    actual_num_samples = min(IMPEDANCE_MAX_SAMPLES, num_periods * samples_per_period)
    slow_100hz_mode = frequency_hz == 100 and samples_per_period == 8
    paced_mode = (
        frequency_hz <= 300 or frequency_hz == 1000 or frequency_hz == 2000
    ) and not slow_100hz_mode

    target_step_ns = 0.0
    if paced_mode:
        denom = frequency_hz * samples_per_period
        target_step_ns = (1_000_000_000.0 + denom / 2.0) / denom

    period_sum_s = 0
    period_sum_c = 0
    for phase_idx in range(samples_per_period):
        idx = (phase_idx * len(INTAN_SINE64)) // samples_per_period
        idx = min(idx, len(INTAN_SINE64) - 1)
        period_sum_s += INTAN_SINE64[idx] - 128
        period_sum_c += INTAN_SINE64[(idx + 16) & 63] - 128

    clear_adc(spi, verbose=False)
    write_register(spi, 2, 0x0040, u_flag=0, m_flag=0, verbose=False)
    write_register(spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)

    reg2 = (channel << 8) | (1 << 6) | (1 << 0) | (scale_bits << 3)
    write_register(spi, 2, reg2, u_flag=0, m_flag=0, verbose=False)
    time.sleep(0.02)

    points: list[dict] = []
    t0_ns = time.monotonic_ns()
    next_step_ns = float(time.monotonic_ns())

    try:
        for _avg_idx in range(num_averages):
            sin_accum = 0
            cos_accum = 0
            for sample_idx in range(actual_num_samples):
                phase_idx = sample_idx % samples_per_period
                idx = (phase_idx * len(INTAN_SINE64)) // samples_per_period
                idx = min(idx, len(INTAN_SINE64) - 1)

                if paced_mode:
                    _wait_until_ns(next_step_ns)
                    next_step_ns += target_step_ns
                elif slow_100hz_mode and sample_idx + 1 < actual_num_samples:
                    time.sleep(0.001)

                dac_val = INTAN_SINE64[idx]
                write_register(spi, 3, dac_val & 0xFF, u_flag=0, m_flag=0, verbose=False)
                adc_val = convert(channel)
                centered = int(adc_val) - 32768
                sin_basis = (
                    (INTAN_SINE64[idx] - 128) * samples_per_period - period_sum_s
                )
                cos_basis = (
                    (INTAN_SINE64[(idx + 16) & 63] - 128) * samples_per_period
                    - period_sum_c
                )
                sin_accum += centered * sin_basis
                cos_accum += centered * cos_basis

            points.append({"sin_accum": sin_accum, "cos_accum": cos_accum})
    finally:
        write_register(spi, 2, reg2 & 0xFFFE, u_flag=0, m_flag=0, verbose=False)
        write_register(spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)

    elapsed_ns = max(1, time.monotonic_ns() - t0_ns)
    eff_freq_millihz = (
        num_averages * actual_num_samples * 1_000_000_000_000
    ) / elapsed_ns / samples_per_period

    return {
        "points": points,
        "actual_num_averages": num_averages,
        "actual_num_samples": actual_num_samples,
        "samples_per_period": samples_per_period,
        "effective_frequency_hz": eff_freq_millihz / 1000.0,
    }
