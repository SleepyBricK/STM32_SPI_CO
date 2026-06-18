#!/usr/bin/env python3
"""
Проверка полной SPI-линии Intan (CS/SCK/MOSI/MISO) во время SPI_STREAM_REAL (DMA timslot).

Ожидание для INTAN_CS_HW_NSS + DMA:
  - 1 CS↓ на каждый 32-bit слот (CONVERT)
  - SCK/MOSI/MISO активны только пока CS↓
  - между слотами CS↑ (pulsed NSS, не один длинный burst)
  - MISO меняется (ответ RHS2116)

API Moku LA: get_data = 1024 точки; частота = 1024/(t2−t1).
  Окно ±2.5 ms → ~200 kSa/s (алиасинг на pscl=8).
  **Окно ±2.048 µs → 125 MSa/s** — см. tools/moku_la_spi_hires.py

  python3 tools/moku_la_spi_hires.py --pscl 8
  python3 tools/moku_la_spi_line_check.py --pscl 32 --stream-ch 2
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

FRAME_HDR = struct.Struct("<IHHIIIIII")
DEFAULT_MOKU = "mokugo-002464.local"
SCK_HZ_BASE = 200_000_000  # PLL2P / prescaler input
SLOT_SCK_CYCLES = 42  # INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES
MIDI_DEFAULT = 15


def digitalize(samples: list[float]) -> np.ndarray:
    out: list[int] = []
    prev = 1
    for v in samples:
        if v >= 0.75:
            out.append(1)
            prev = 1
        elif v <= 0.25:
            out.append(0)
            prev = 0
        else:
            out.append(1 - prev)
            prev = 1 - prev
    return np.array(out, dtype=np.int8)


def edges(sig: np.ndarray) -> int:
    return int(np.sum(np.diff(sig) != 0)) if len(sig) > 1 else 0


def cs_windows(cs: np.ndarray) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Индексы [start,end) для CS low и CS high сегментов."""
    low: list[tuple[int, int]] = []
    high: list[tuple[int, int]] = []
    i = 0
    while i < len(cs):
        val = cs[i]
        j = i
        while j < len(cs) and cs[j] == val:
            j += 1
        (low if val == 0 else high).append((i, j))
        i = j
    return low, high


def activity_in_windows(sig: np.ndarray, windows: list[tuple[int, int]]) -> dict:
    if not windows:
        return {"windows": 0, "edges_mean": 0.0, "edges_max": 0, "active_frac": 0.0}
    e_list: list[int] = []
    active = 0
    for a, b in windows:
        seg = sig[a:b]
        if len(seg) < 2:
            e_list.append(0)
            continue
        e = edges(seg)
        e_list.append(e)
        if e > 0:
            active += 1
    return {
        "windows": len(windows),
        "edges_mean": float(np.mean(e_list)),
        "edges_max": int(max(e_list)),
        "active_frac": active / len(windows),
    }


def analyze_spi_line(t: np.ndarray, pins: dict[str, list[float]], *, wall_ksps: float, pscl: int, midi: int) -> dict:
    cs = digitalize(pins["cs"])
    sck = digitalize(pins["sck"])
    mosi = digitalize(pins["mosi"])
    miso = digitalize(pins["miso"])

    span_s = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    la_fs = len(t) / span_s if span_s > 0 else 0.0
    sck_hz = SCK_HZ_BASE / pscl
    slot_us = SLOT_SCK_CYCLES / sck_hz * 1e6
    cs_rate_expected = wall_ksps * 1000.0 if wall_ksps else 0.0

    dcs = np.diff(cs)
    cs_falls = int(np.sum(dcs == -1))
    cs_rises = int(np.sum(dcs == 1))

    low_w, high_w = cs_windows(cs)
    low_lens = [b - a for a, b in low_w]
    high_lens = [b - a for a, b in high_w]

    cs_low_us: list[float] = []
    for a, b in low_w:
        if b > a:
            cs_low_us.append(float((t[b - 1] - t[a]) * 1e6 + (t[1] - t[0]) * 1e6 if len(t) > 1 else 0))

    sck_in_cs_low = activity_in_windows(sck, low_w)
    sck_in_cs_high = activity_in_windows(sck, high_w)
    mosi_in_cs_low = activity_in_windows(mosi, low_w)
    miso_in_cs_low = activity_in_windows(miso, low_w)

    # Нарушения протокола (на разрешении LA)
    cs_without_sck = sum(
        1 for a, b in low_w if b - a >= 2 and edges(sck[a:b]) == 0
    )
    long_cs_low = sum(1 for L in low_lens if L >= 4)  # ≥4 samples ≈20 µs при 204 kS/s
    sck_outside_cs = sck_in_cs_high["active_frac"]

    cs_rate_meas = cs_falls / span_s if span_s > 0 else 0.0
    cs_vs_stream = cs_rate_meas / cs_rate_expected if cs_rate_expected > 0 else 0.0

    checks: dict[str, str] = {}

    if cs_falls < 10:
        checks["cs_pulses"] = "FAIL" if wall_ksps > 0.4 * la_fs / 1000 else "WARN_ALIAS"
    elif max(low_lens or [0]) <= 2:
        checks["cs_width"] = "OK"
    else:
        checks["cs_width"] = "FAIL_LONG_CS"

    if sck_in_cs_low["active_frac"] >= 0.5 and sck_in_cs_high["active_frac"] < 0.3:
        checks["sck_gated_by_cs"] = "OK"
    elif sck_in_cs_low["edges_mean"] > sck_in_cs_high["edges_mean"]:
        checks["sck_gated_by_cs"] = "OK_WEAK"
    else:
        checks["sck_gated_by_cs"] = "FAIL"

    if mosi_in_cs_low["active_frac"] >= 0.4:
        checks["mosi_active"] = "OK"
    elif mosi_in_cs_low["edges_mean"] > 0:
        checks["mosi_active"] = "OK_WEAK"
    else:
        checks["mosi_active"] = "FAIL"

    if miso_in_cs_low["active_frac"] >= 0.3:
        checks["miso_response"] = "OK"
    elif miso_in_cs_low["edges_mean"] > 0:
        checks["miso_response"] = "OK_WEAK"
    else:
        checks["miso_response"] = "FAIL"

    if cs_without_sck == 0:
        checks["cs_has_sck"] = "OK"
    elif cs_without_sck <= max(1, len(low_w) // 20):
        checks["cs_has_sck"] = "WARN"
    else:
        checks["cs_has_sck"] = "FAIL"

    if long_cs_low == 0:
        checks["no_grouped_burst"] = "OK"
    else:
        checks["no_grouped_burst"] = "FAIL"

    if 0.3 <= cs_vs_stream <= 1.5 or cs_falls < 10:
        checks["cs_rate_vs_stream"] = "OK" if cs_falls >= 10 else "WARN_ALIAS"
    else:
        checks["cs_rate_vs_stream"] = "WARN"

    fails = [k for k, v in checks.items() if v.startswith("FAIL")]
    verdict = "spi_line_ok" if not fails else ("aliased" if cs_falls < 10 and wall_ksps * 1000 > 0.4 * la_fs else "spi_line_bad")

    return {
        "span_ms": span_s * 1e3,
        "la_fs_khz": la_fs / 1000,
        "wall_ksps": wall_ksps,
        "pscl": pscl,
        "midi": midi,
        "sck_mhz": sck_hz / 1e6,
        "slot_us_expected": slot_us,
        "cs_falls": cs_falls,
        "cs_rises": cs_rises,
        "cs_rate_khz": cs_rate_meas / 1000,
        "cs_vs_stream_ratio": cs_vs_stream,
        "cs_low_us_med": float(np.median(cs_low_us)) if cs_low_us else None,
        "cs_low_samples_max": max(low_lens) if low_lens else 0,
        "cs_high_samples_max": max(high_lens) if high_lens else 0,
        "sck_total_edges": edges(sck),
        "sck_in_cs_low": sck_in_cs_low,
        "sck_in_cs_high": sck_in_cs_high,
        "mosi_in_cs_low": mosi_in_cs_low,
        "miso_in_cs_low": miso_in_cs_low,
        "cs_without_sck": cs_without_sck,
        "long_cs_low_windows": long_cs_low,
        "checks": checks,
        "verdict": verdict,
    }


def stream_worker(
    *,
    ch: int,
    n: int,
    pscl: int,
    midi: int,
    spi_started: threading.Event,
    done: threading.Event,
    out: dict,
) -> None:
    try:
        dev, ifn = open_device(reset=False)
        for cmd in (
            "STOP",
            f"SPI_PSCL {pscl}",
            f"NSS_MIDI {midi}",
            "WRITE 42 0 1 0",
            "INIT_RECORD 350000",
            "CLEAR_ADC",
        ):
            run_text_command(dev, cmd, timeout_ms=30_000, drain_before=True)
        reply = run_text_command(
            dev, f"SPI_STREAM_REAL {n} {ch} 0", timeout_ms=60_000, drain_before=True
        )
        if not reply.startswith("OK"):
            raise RuntimeError(reply)
        spi_started.set()
        out["stream_active"] = True
        got = 0
        t0 = time.perf_counter()
        while got < n:
            pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60_000))
            _, _, _, _, _, sc, spi_ovf, usb_ovf, _ = FRAME_HDR.unpack_from(pkt, 0)
            if spi_ovf or usb_ovf:
                raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
            got += sc
            out["wall_ksps"] = got / (time.perf_counter() - t0) / 1000.0
        out["samples"] = got
        run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
        close_device(dev, ifn)
    except Exception as e:
        out["error"] = str(e)
    finally:
        done.set()


def capture_during_stream(
    moku: str,
    *,
    pscl: int,
    midi: int,
    stream_ch: int,
    stream_n: int,
    snaps: int,
) -> dict:
    from moku.instruments import LogicAnalyzer

    spi_started = threading.Event()
    done = threading.Event()
    stream_info: dict = {}

    la = LogicAnalyzer(moku, force_connect=True, persist_state=False)
    la.session.read_timeout = 120
    try:
        la.set_defaults()
        for p in (1, 2, 3, 5):
            la.set_pin_mode(p, "I")
        la.set_timebase(-0.0025, 0.0025)
        la.set_trigger(pins=[{"pin": 1, "edge": "Falling"}], mode="Auto")

        th = threading.Thread(
            target=stream_worker,
            kwargs={
                "ch": stream_ch,
                "n": stream_n,
                "pscl": pscl,
                "midi": midi,
                "spi_started": spi_started,
                "done": done,
                "out": stream_info,
            },
            daemon=True,
        )
        th.start()
        if not spi_started.wait(60):
            stream_info["error"] = "SPI_STREAM timeout"

        analyses: list[dict] = []
        for _ in range(snaps):
            d = la.get_data(
                wait_reacquire=True,
                wait_complete=True,
                include_pins=[1, 2, 3, 5],
                timeout=60,
            )
            t = np.array(d["time"])
            pins = {
                "cs": d["pin1"],
                "sck": d["pin2"],
                "mosi": d["pin3"],
                "miso": d["pin5"],
            }
            analyses.append(
                analyze_spi_line(
                    t,
                    pins,
                    wall_ksps=stream_info.get("wall_ksps", 0.0),
                    pscl=pscl,
                    midi=midi,
                )
            )

        best = max(analyses, key=lambda x: x["cs_falls"])
        return {
            "stream": stream_info,
            "best_frame": best,
            "n_frames": len(analyses),
            "model": {
                "path": "SPI_STREAM_REAL → DMA timslot (HW NSS PA11)",
                "tx_per_sample": "1× CONVERT 32-bit word",
                "cs_per_word": 1,
                "slot_sck_cycles": SLOT_SCK_CYCLES,
                "midi_cycles": midi,
            },
        }
    finally:
        done.wait(timeout=90)
        th.join(timeout=10)
        la.relinquish_ownership()


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku LA: полная SPI-линия во время DMA stream")
    ap.add_argument("--moku", default=DEFAULT_MOKU)
    ap.add_argument("--pscl", type=int, default=32)
    ap.add_argument("--midi", type=int, default=15)
    ap.add_argument("--stream-ch", type=int, default=2)
    ap.add_argument("--stream-n", type=int, default=150_000)
    ap.add_argument("--snaps", type=int, default=12)
    ap.add_argument("--compare", default="", help="pscl через запятую, напр. 8,16,32")
    args = ap.parse_args()

    if args.compare:
        pscls = [int(x.strip()) for x in args.compare.split(",")]
        results = [
            capture_during_stream(
                args.moku,
                pscl=p,
                midi=args.midi,
                stream_ch=args.stream_ch,
                stream_n=args.stream_n,
                snaps=args.snaps,
            )
            for p in pscls
        ]
    else:
        results = [
            capture_during_stream(
                args.moku,
                pscl=args.pscl,
                midi=args.midi,
                stream_ch=args.stream_ch,
                stream_n=args.stream_n,
                snaps=args.snaps,
            )
        ]

    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
