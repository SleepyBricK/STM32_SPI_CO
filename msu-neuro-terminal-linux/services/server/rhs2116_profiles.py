#!/usr/bin/env python3
"""
RHS2116 mode profiles and register invariants shared by TCP/UDP/helper paths.

This module deliberately contains only pure data/helpers so it can be reused by
different runtime paths without creating import cycles.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

RHS2116_CHIP_ID_REG = 255
RHS2116_CHIP_ID_VALUE = 32

RHS2116_STIM_UNLOCK_REG32 = 0xAAAA
RHS2116_STIM_UNLOCK_REG33 = 0x00FF

RHS2116_REG2_ZCHECK_OFF = 0x0000
RHS2116_REG3_ZCHECK_NEUTRAL = 0x0080
RHS2116_REG38_DC_AMP_POWER_ALL_ON = 0xFFFF
RHS2116_REG8_AC_AMP_POWER_ALL_ON = 0xFFFF

RHS2116_REG10_FAST_SETTLE_OFF = 0x0000
RHS2116_REG12_USE_FL_A_ALL = 0xFFFF

RHS2116_STIM_STEP_1UA = 0x00E2
RHS2116_STIM_BIAS_1UA = 0x00AA
RHS2116_STIM_VRECOV_0V = 0x0080
RHS2116_STIM_IMAX_1NA = 0x4F00
RHS2116_STIM_CURRENT_ZERO = 0x8000

RHS2116_STIM_REG1 = 0x051A
RHS2116_STIM_REG4 = 0x0016
RHS2116_STIM_REG5 = 0x0017
RHS2116_STIM_REG6 = 0x00A8
RHS2116_STIM_REG7 = 0x000A

RHS2116_RECORDING_REG1 = 0x051A
RHS2116_RECORDING_REG4 = 0x0016
RHS2116_RECORDING_REG5 = 0x0017
RHS2116_RECORDING_REG6 = 0x00A8
RHS2116_RECORDING_REG7 = 0x000A

SequenceCommand = Tuple[str, object]


def rhs2116_validate_channels(channels: Iterable[int]) -> List[int]:
    validated = sorted({int(ch) for ch in channels})
    for ch in validated:
        if ch < 0 or ch > 15:
            raise ValueError(f"RHS2116 channel out of range: {ch}")
    return validated


def rhs2116_channel_mask(channels: Sequence[int]) -> int:
    mask = 0
    for channel in rhs2116_validate_channels(channels):
        mask |= 1 << channel
    return mask & 0xFFFF


def rhs2116_current_word(current_value: int) -> int:
    current_val = min(max(int(current_value), 0), 255)
    return RHS2116_STIM_CURRENT_ZERO | current_val


def rhs2116_register0_for_adc_rate(adc_sampling_rate_ksps: float) -> int:
    rate = float(adc_sampling_rate_ksps)
    if rate <= 120:
        adc_buffer_bias, mux_bias = 32, 40
    elif rate <= 140:
        adc_buffer_bias, mux_bias = 16, 40
    elif rate <= 175:
        adc_buffer_bias, mux_bias = 8, 40
    elif rate <= 220:
        adc_buffer_bias, mux_bias = 8, 32
    elif rate <= 280:
        adc_buffer_bias, mux_bias = 8, 26
    elif rate <= 350:
        adc_buffer_bias, mux_bias = 4, 18
    elif rate <= 440:
        adc_buffer_bias, mux_bias = 3, 16
    else:
        adc_buffer_bias, mux_bias = 3, 5
    return ((adc_buffer_bias & 0xFF) << 8) | (mux_bias & 0xFF)


def rhs2116_recording_init_commands(adc_sampling_rate_ksps: float) -> List[Tuple]:
    return [
        ("read", RHS2116_CHIP_ID_REG),
        ("sleep", 0.001),
        ("write", 32, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 33, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 38, RHS2116_REG38_DC_AMP_POWER_ALL_ON, 0, 0),
        ("sleep", 0.001),
        ("clear_adc",),
        ("sleep", 0.001),
        ("write", 0, rhs2116_register0_for_adc_rate(adc_sampling_rate_ksps), 0, 0),
        ("sleep", 0.001),
        ("write", 1, RHS2116_RECORDING_REG1, 0, 0),
        ("sleep", 0.001),
        ("write", 2, RHS2116_REG2_ZCHECK_OFF, 0, 0),
        ("sleep", 0.001),
        ("write", 3, RHS2116_REG3_ZCHECK_NEUTRAL, 0, 0),
        ("sleep", 0.001),
        ("write", 4, RHS2116_RECORDING_REG4, 0, 0),
        ("sleep", 0.001),
        ("write", 5, RHS2116_RECORDING_REG5, 0, 0),
        ("sleep", 0.001),
        ("write", 6, RHS2116_RECORDING_REG6, 0, 0),
        ("sleep", 0.001),
        ("write", 7, RHS2116_RECORDING_REG7, 0, 0),
        ("sleep", 0.001),
        ("write", 8, RHS2116_REG8_AC_AMP_POWER_ALL_ON, 0, 0),
        ("sleep", 0.001),
        ("write", 10, RHS2116_REG10_FAST_SETTLE_OFF, 0, 0),
        ("sleep", 0.001),
        ("write", 12, RHS2116_REG12_USE_FL_A_ALL, 0, 0),
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


def rhs2116_stimulation_init_commands(adc_sampling_rate_ksps: float = 480.0) -> List[Tuple]:
    commands = [
        ("read", RHS2116_CHIP_ID_REG),
        ("sleep", 0.001),
        ("write", 32, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 33, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 38, RHS2116_REG38_DC_AMP_POWER_ALL_ON, 0, 0),
        ("sleep", 0.001),
        ("clear_adc",),
        ("sleep", 0.001),
        ("write", 0, rhs2116_register0_for_adc_rate(adc_sampling_rate_ksps), 0, 0),
        ("sleep", 0.001),
        ("write", 1, RHS2116_STIM_REG1, 0, 0),
        ("sleep", 0.001),
        ("write", 2, RHS2116_REG2_ZCHECK_OFF, 0, 0),
        ("sleep", 0.001),
        ("write", 3, RHS2116_REG3_ZCHECK_NEUTRAL, 0, 0),
        ("sleep", 0.001),
        ("write", 4, RHS2116_STIM_REG4, 0, 0),
        ("sleep", 0.001),
        ("write", 5, RHS2116_STIM_REG5, 0, 0),
        ("sleep", 0.001),
        ("write", 6, RHS2116_STIM_REG6, 0, 0),
        ("sleep", 0.001),
        ("write", 7, RHS2116_STIM_REG7, 0, 0),
        ("sleep", 0.001),
        ("write", 8, RHS2116_REG8_AC_AMP_POWER_ALL_ON, 0, 0),
        ("sleep", 0.001),
        ("write", 10, RHS2116_REG10_FAST_SETTLE_OFF, 0, 0),
        ("sleep", 0.001),
        ("write", 12, RHS2116_REG12_USE_FL_A_ALL, 0, 0),
        ("sleep", 0.001),
        ("write", 34, RHS2116_STIM_STEP_1UA, 0, 0),
        ("sleep", 0.001),
        ("write", 35, RHS2116_STIM_BIAS_1UA, 0, 0),
        ("sleep", 0.001),
        ("write", 36, RHS2116_STIM_VRECOV_0V, 0, 0),
        ("sleep", 0.001),
        ("write", 37, RHS2116_STIM_IMAX_1NA, 0, 0),
        ("sleep", 0.001),
        ("write", 44, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 46, 0x0000, 0, 0),
        ("sleep", 0.001),
        ("write", 48, 0x0000, 0, 0),
        ("sleep", 0.001),
    ]
    for channel in range(16):
        commands.append(("write", 64 + channel, RHS2116_STIM_CURRENT_ZERO, 0, 0))
        commands.append(("write", 96 + channel, RHS2116_STIM_CURRENT_ZERO, 0, 0))
    commands.extend([
        ("sleep", 0.001),
        ("write", 42, 0x0000, 1, 0),
        ("sleep", 0.001),
        ("write", 32, RHS2116_STIM_UNLOCK_REG32, 0, 0),
        ("sleep", 0.001),
        ("write", 33, RHS2116_STIM_UNLOCK_REG33, 0, 0),
        ("sleep", 0.001),
        ("clear_compliance",),
        ("sleep", 0.001),
    ])
    return commands


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
    read_register,
    write_register,
    clear_adc,
    clear_compliance,
    sleep_fn,
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
        else:
            raise ValueError(f"Unknown RHS2116 sequence op: {op}")
