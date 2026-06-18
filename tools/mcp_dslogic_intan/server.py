#!/usr/bin/env python3
"""
MCP: DSLogic U2Basic + STM32 Intan SPI.

U2Basic (PID 0x0029) не виден в sigrok-cli — захват LA через DSView GUI
или через libsigrok4DSL (см. github_survey / agent-dsviewer-logic-analyzer).

Cursor: .cursor/mcp.json → dslogic-intan
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from lib import (
    analyze_dsview_csv,
    environment_status,
    github_survey,
    run_stm32_stream,
    scan_usb_devices,
)

mcp = FastMCP(
    name="dslogic-intan",
    instructions=(
        "DSLogic (DreamSourceLab) + STM32 Intan RHS2116. "
        "U2Basic требует DSView для захвата; STM32 stream через stm32_intan_stream. "
        "Экспорт CSV из DSView → analyze_dsview_csv для проверки pulsed NSS."
    ),
)


@mcp.tool()
def dslogic_usb_scan() -> str:
    """Скан USB: DSLogic, STM32 stream 0483:5741, совместимость с sigrok."""
    return json.dumps(scan_usb_devices(), indent=2, ensure_ascii=False)


@mcp.tool()
def dslogic_environment() -> str:
    """Полный статус: USB, DSView, sigrok-cli, рекомендации по захвату."""
    return json.dumps(environment_status(), indent=2, ensure_ascii=False)


@mcp.tool()
def github_mcp_survey() -> str:
    """Обзор GitHub MCP для logic analyzer (sigrok / libsigrok4DSL / DSView)."""
    return github_survey()


@mcp.tool()
def stm32_intan_stream(
    channel: int = 2,
    samples: int = 1_500_000,
    pscl: int = 8,
    nss_midi: int = 15,
    reset_usb: bool = False,
) -> str:
    """
    Запуск SPI_STREAM_REAL на STM32 с drain bulk (для синхронного захвата в DSView).
    channel: Intan ADC channel 0..15. samples: число сэмплов.
    """
    return json.dumps(
        run_stm32_stream(
            ch=channel,
            samples=samples,
            pscl=pscl,
            midi=nss_midi,
            reset_usb=reset_usb,
        ),
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
def analyze_dsview_csv_tool(
    file_path: str,
    cs_channel: int = 3,
) -> str:
    """
    Анализ CSV из DSView: переключения NSS (D3 по умолчанию).
    file_path: путь к экспортированному CSV.
    """
    return json.dumps(
        analyze_dsview_csv(file_path, cs_channel=cs_channel),
        indent=2,
        ensure_ascii=False,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
