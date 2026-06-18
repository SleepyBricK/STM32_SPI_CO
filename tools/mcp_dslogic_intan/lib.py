"""DSLogic U2Basic + STM32 Intan — общая логика для MCP."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

DSLOGIC_VID = 0x2A0E
STM32_STREAM_PID = 0x5741

# sigrok.org/wiki/DreamSourceLab_DSLogic — Instrument v2
SIGROK_UNSUPPORTED_PIDS: dict[int, str] = {
    0x0029: "DSLogic U2Basic",
    0x002A: "DSLogic U3Pro16",
    0x002C: "DSLogic U3Pro32",
    0x002D: "DSLogic U2Pro16",
    0x0030: "DSLogic Plus (pgl12)",
    0x0031: "DSLogic U2Basic (pgl12)",
    0x0034: "DSLogic Plus (pgl12-2)",
    0x0035: "DSLogic U2Basic (pgl12-2)",
}

SIGROK_SUPPORTED_PIDS: dict[int, str] = {
    0x0001: "DSLogic",
    0x0002: "DSCope",
    0x0003: "DSLogic Pro",
    0x0020: "DSLogic Plus",
    0x0021: "DSLogic Basic",
}

GITHUB_MCP_REPOS = [
    {
        "name": "agent-dsviewer-logic-analyzer",
        "url": "https://github.com/felixfinal/agent-dsviewer-logic-analyzer",
        "backend": "dslogic-cli + libsigrok4DSL (из DSView)",
        "u2basic": "Да, если собрать libsigrok4DSL и dslogic-cli (Linux; на Mac — DSView линкует lib статически)",
        "notes": "Лучший вариант для U2Basic через MCP: native capture, SPI decode.",
    },
    {
        "name": "logicanalyzer-mcp",
        "url": "https://github.com/DatanoiseTV/logicanalyzer-mcp",
        "backend": "Go + libsigrok4DSL submodule",
        "u2basic": "Возможно через HAL dslogic",
        "notes": "DSLogic-first, 18 MCP tools, тяжёлая сборка.",
    },
    {
        "name": "logic-analyzer-mcp",
        "url": "https://github.com/sandraschi/logic-analyzer-mcp",
        "backend": "sigrok-cli subprocess",
        "u2basic": "Нет (PID 0x0029 не в libsigrok)",
        "notes": "Симулятор + sigrok; для U2Basic не подходит.",
    },
    {
        "name": "mcp-sigrok",
        "url": "https://github.com/daedalus/mcp-sigrok",
        "backend": "sigrok-cli",
        "u2basic": "Нет",
        "notes": "Обёртка всех команд sigrok-cli.",
    },
]

INTAN_LA_WIRING = {
    "D0": "SCK  (PA9)",
    "D1": "MISO (PB14)",
    "D2": "MOSI (PC1)",
    "D3": "NSS  (PA11, active low, pulsed между 32-bit кадрами)",
}


def _find_dsview() -> Path | None:
    for p in (
        Path("/Applications/DSView.app/Contents/MacOS/DSView"),
        Path.home() / "Applications/DSView.app/Contents/MacOS/DSView",
    ):
        if p.is_file():
            return p
    return None


def _find_sigrok_cli() -> str | None:
    for p in ("/opt/homebrew/bin/sigrok-cli", "/usr/local/bin/sigrok-cli"):
        if Path(p).is_file():
            return p
    return shutil.which("sigrok-cli")


def scan_usb_devices() -> dict:
    import usb.core
    import usb.util

    out: dict = {"dslogic": None, "stm32_stream": None, "stlink": None, "other": []}
    for dev in usb.core.find(find_all=True):
        try:
            name = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
        except Exception:
            name = ""
        entry = {
            "vid": f"{dev.idVendor:04x}",
            "pid": f"{dev.idProduct:04x}",
            "product": name,
        }
        if dev.idVendor == DSLOGIC_VID:
            pid = dev.idProduct
            entry["model"] = SIGROK_UNSUPPORTED_PIDS.get(pid) or SIGROK_SUPPORTED_PIDS.get(pid) or "DreamSourceLab"
            entry["sigrok_capture"] = pid in SIGROK_SUPPORTED_PIDS
            entry["capture_via"] = (
                "sigrok-cli"
                if pid in SIGROK_SUPPORTED_PIDS
                else "DSView GUI или libsigrok4DSL (agent-dsviewer-logic-analyzer)"
            )
            out["dslogic"] = entry
        elif dev.idVendor == 0x0483 and dev.idProduct == STM32_STREAM_PID:
            out["stm32_stream"] = entry
        elif dev.idVendor == 0x0483 and dev.idProduct == 0x3748:
            out["stlink"] = entry
        elif dev.idVendor in (DSLOGIC_VID, 0x0483):
            out["other"].append(entry)
    return out


def environment_status() -> dict:
    usb = scan_usb_devices()
    sigrok = _find_sigrok_cli()
    dsview = _find_dsview()
    sigrok_scan = ""
    if sigrok and usb.get("dslogic", {}) and usb["dslogic"].get("sigrok_capture"):
        try:
            r = subprocess.run([sigrok, "--scan"], capture_output=True, text=True, timeout=10)
            sigrok_scan = r.stdout.strip()
        except Exception as e:
            sigrok_scan = f"error: {e}"

    return {
        "usb": usb,
        "dsview_installed": dsview is not None,
        "dsview_path": str(dsview) if dsview else None,
        "sigrok_cli": sigrok,
        "sigrok_scan": sigrok_scan or None,
        "intan_la_wiring": INTAN_LA_WIRING,
        "recommendation": _recommendation(usb, dsview, sigrok),
    }


def _recommendation(usb: dict, dsview: Path | None, sigrok: str | None) -> str:
    dl = usb.get("dslogic")
    if not dl:
        return "DSLogic не на USB. Подключите LA и закройте DSView перед sigrok (если поддерживается)."
    if not dl.get("sigrok_capture"):
        if dsview:
            return (
                f"{dl.get('model')}: захват только через DSView GUI; "
                "экспорт CSV → analyze_dsview_csv; stream STM32 → stm32_intan_stream."
            )
        return f"{dl.get('model')}: установите DSView или соберите agent-dsviewer-logic-analyzer."
    if not sigrok:
        return "Установите: brew install sigrok-cli"
    return "Можно sigrok-cli capture или mcp-sigrok / logic-analyzer-mcp."


def run_stm32_stream(
    ch: int = 2,
    samples: int = 1_500_000,
    pscl: int = 8,
    midi: int = 15,
    ksps: int = 350_000,
    reset_usb: bool = False,
) -> dict:
    from dslogic_intan_spi import stm32_stream_drain

    if scan_usb_devices().get("stm32_stream") is None:
        return {"ok": False, "error": "STM32 stream 0483:5741 не найден (нужен HS USB, не только ST-Link)"}
    try:
        stm32_stream_drain(
            ch,
            samples,
            ksps=ksps,
            pscl=pscl,
            midi=midi,
            reset_usb=reset_usb,
        )
        return {"ok": True, "channel": ch, "samples": samples, "pscl": pscl, "nss_midi": midi}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def analyze_dsview_csv(
    path: str,
    cs_channel: int = 3,
    *,
    max_rows: int = 5_000_000,
) -> dict:
    """Подсчёт переключений CS в CSV, экспортированном из DSView (logic channels)."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"file not found: {p}"}

    edges = 0
    rows = 0
    prev: int | None = None
    low_pulses = 0
    in_low = False

    with p.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return {"ok": False, "error": "empty csv"}

        # DSView/sigrok CSV: sample, time, ch0, ch1, ...
        # Индекс колонки CS: 2 + cs_channel (после sample, time)
        col = 2 + cs_channel
        if col >= len(header):
            return {"ok": False, "error": f"column {col} missing; header={header}"}

        for row in reader:
            rows += 1
            if rows > max_rows:
                break
            try:
                val = int(float(row[col]))
            except (ValueError, IndexError):
                continue
            if prev is not None and val != prev:
                edges += 1
                if val == 0 and prev == 1:
                    in_low = True
                elif val == 1 and prev == 0 and in_low:
                    low_pulses += 1
                    in_low = False
            prev = val

    return {
        "ok": True,
        "file": str(p),
        "rows_analyzed": rows,
        "cs_channel": f"D{cs_channel}",
        "cs_edges": edges,
        "cs_low_pulses": low_pulses,
        "interpretation": (
            "pulsed NSS OK: cs_low_pulses >> 1 (много коротких CS↓ на 32-bit кадры). "
            "Плохо: 0–1 импульс на весь захват (длинный CS на burst)."
        ),
    }


def github_survey() -> str:
    return json.dumps(GITHUB_MCP_REPOS, indent=2, ensure_ascii=False)
