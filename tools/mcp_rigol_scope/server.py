#!/usr/bin/env python3
"""MCP: Rigol DHO804 (и другие Rigol SCPI) + Intan stim измерения."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from lib import (
    INTAN_STIM_WIRING,
    capture_summary,
    configure_scope,
    measure_pulses,
    run_scope,
    scan_rigol,
    scope_status,
    stop_scope,
)

mcp = FastMCP(
    name="rigol-scope",
    instructions=(
        "Rigol осциллограф по USB/SCPI (PyVISA). "
        "DHO804 на Mac: USB0::6833::1101::<serial>::0::INSTR. "
        "Для Intan stim: CH1 на нагрузку 10 kΩ; single-shot capture ждёт acquisition complete и читает RAW waveform."
    ),
)


@mcp.tool()
def rigol_scan() -> str:
    """Скан VISA/USB: найти Rigol и вернуть resource string для остальных tools."""
    return json.dumps(scan_rigol(), indent=2, ensure_ascii=False)


@mcp.tool()
def rigol_status(resource: str = "") -> str:
    """Текущие timebase/trigger/каналы осциллографа."""
    return json.dumps(scope_status(resource or None), indent=2, ensure_ascii=False)


@mcp.tool()
def rigol_configure(
    channel: int = 1,
    scale_v: float = 0.2,
    offset_v: float = 0.0,
    coupling: str = "DC",
    time_scale_s: float = 0.01,
    time_offset_s: float = 0.0,
    trigger_mode: str = "EDGE",
    trigger_source: str = "CHAN1",
    trigger_level_v: float = 0.1,
    trigger_slope: str = "POS",
    trigger_sweep: str = "AUTO",
    resource: str = "",
) -> str:
    """
    Настройка канала, развёртки и триггера.
    Пример для Intan stim 180 µA / 10 kΩ: scale_v=0.2, time_scale_s=1e-4, trigger_level_v=0.5.
    """
    return json.dumps(
        configure_scope(
            resource or None,
            channel=channel,
            scale_v=scale_v,
            offset_v=offset_v,
            coupling=coupling,
            time_scale_s=time_scale_s,
            time_offset_s=time_offset_s,
            trigger_mode=trigger_mode,
            trigger_source=trigger_source,
            trigger_level_v=trigger_level_v,
            trigger_slope=trigger_slope,
            trigger_sweep=trigger_sweep,
        ),
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def rigol_capture(
    channel: int = 1,
    points: int = 1000,
    wait_s: float = 0.5,
    single: bool = False,
    save_csv: str = "",
    resource: str = "",
) -> str:
    """Захват waveform RAW: min/max/pp, опционально CSV (time_s,voltage_v)."""
    return json.dumps(
        capture_summary(
            resource or None,
            channel=channel,
            points=points,
            wait_s=wait_s,
            single=single,
            save_csv=save_csv or None,
        ),
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def rigol_measure_pulses(
    channel: int = 1,
    points: int = 4000,
    threshold_v: float = -1.0,
    wait_s: float = 2.0,
    single: bool = True,
    resource: str = "",
) -> str:
    """
    Измерить ширины HIGH-импульсов на канале (для PATTERN stim).
    threshold_v < 0 → авто-порог по median+45% ptp.
    """
    thr = None if threshold_v < 0 else threshold_v
    return json.dumps(
        measure_pulses(
            resource or None,
            channel=channel,
            points=points,
            threshold_v=thr,
            wait_s=wait_s,
            single=single,
        ),
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def rigol_run(resource: str = "") -> str:
    """Запустить непрерывный RUN на осциллографе."""
    return json.dumps(run_scope(resource or None), indent=2, ensure_ascii=False)


@mcp.tool()
def rigol_stop(resource: str = "") -> str:
    """Остановить захват (STOP)."""
    return json.dumps(stop_scope(resource or None), indent=2, ensure_ascii=False)


@mcp.tool()
def rigol_intan_wiring() -> str:
    """Подсказка по подключению Rigol CH1 к Intan stim (10 kΩ load)."""
    return json.dumps(INTAN_STIM_WIRING, indent=2, ensure_ascii=False)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
