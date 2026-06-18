#!/usr/bin/env python3
"""
Moku LA + SPI_STREAM_REAL: диагностика SPI во время стрима.

Важно: API Moku LA отдаёт ~1024 точки / ~5 ms (~200 kS/s). При pscl=8
(CS ~385 kHz) импульсы алиасируются и выглядят как «CS залип HIGH».
Для автоматической проверки NSS используйте --pscl 16 или 32.

  python3 tools/moku_la_stream_diag.py
  python3 tools/moku_la_stream_diag.py --pscl 16 --stream-n 200000
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
CHUNK_AGG = 8188  # SPI_STREAM_AGG_MAX в usb_stream_service.c


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


def analyze_cs(t: np.ndarray, raw: list[float]) -> dict:
    cs = digitalize(raw)
    span_s = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    dcs = np.diff(cs)
    fall = np.where(dcs == -1)[0]
    rise = np.where(dcs == 1)[0]
    widths_us: list[float] = []
    for f in fall:
        r = rise[rise > f]
        if len(r):
            widths_us.append(float((t[r[0]] - t[f]) * 1e6))
    low_runs: list[int] = []
    i = 0
    while i < len(cs):
        if cs[i] == 0:
            j = i
            while j < len(cs) and cs[j] == 0:
                j += 1
            low_runs.append(j - i)
            i = j
        else:
            i += 1
    return {
        "span_ms": span_s * 1e3,
        "fs_khz": len(t) / span_s / 1000.0 if span_s > 0 else 0.0,
        "cs_falls": int(len(fall)),
        "cs_duty": float(cs.mean()),
        "low_width_us_med": float(np.median(widths_us)) if widths_us else None,
        "max_low_run_samples": max(low_runs) if low_runs else 0,
        "pulse_rate_khz": len(fall) / span_s / 1000.0 if span_s > 0 else 0.0,
        "unique_raw": sorted(set(raw)),
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


def usb_jump_check(pscl: int, midi: int, ch: int, n: int) -> dict:
    dev, ifn = open_device(reset=False)
    try:
        for cmd in (
            "STOP",
            f"SPI_PSCL {pscl}",
            f"NSS_MIDI {midi}",
            "INIT_RECORD 350000",
            "CLEAR_ADC",
        ):
            run_text_command(dev, cmd, timeout_ms=30_000, drain_before=True)
        run_text_command(dev, f"SPI_STREAM_REAL {n} {ch} 0", timeout_ms=60_000, drain_before=True)
        codes: list[int] = []
        t0 = time.perf_counter()
        while len(codes) < n:
            pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60_000))
            sc = FRAME_HDR.unpack_from(pkt, 0)[5]
            for i in range(sc):
                if len(codes) >= n:
                    break
                codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
        elapsed = time.perf_counter() - t0
        run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
        d = np.diff(np.array(codes, dtype=np.int32))
        jumps = int(np.sum(np.abs(d) > 500))
        ac8188 = 0.0
        if len(d) > CHUNK_AGG * 3:
            a = np.abs(d[: CHUNK_AGG * 3])
            ac8188 = float(np.corrcoef(a, np.roll(a, CHUNK_AGG))[0, 1])
        return {
            "ksps": len(codes) / elapsed / 1000.0,
            "jumps_gt500": jumps,
            "chunk8188_autocorr": ac8188,
        }
    finally:
        close_device(dev, ifn)


def run_diag(
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
            stream_info["error"] = "SPI_STREAM не стартовал за 60 с"

        la_frames: list[dict] = []
        for _ in range(snaps):
            d = la.get_data(
                wait_reacquire=True,
                wait_complete=True,
                include_pins=[1, 2, 3, 5],
                timeout=60,
            )
            t = np.array(d["time"])
            la_frames.append(analyze_cs(t, d["pin1"]))
            sck = digitalize(d["pin2"])
            la_frames[-1]["sck_edges"] = int(np.sum(np.diff(sck) != 0))

        best = max(la_frames, key=lambda x: x["cs_falls"])
        usb = usb_jump_check(pscl, midi, stream_ch, min(80_000, stream_n))

        la_fs = best.get("fs_khz", 0)
        cs_rate = stream_info.get("wall_ksps", 0)
        aliasing_risk = cs_rate > 0.4 * la_fs

        if best["cs_falls"] > 50 and best["max_low_run_samples"] <= 2:
            nss_verdict = "pulsed_nss_ok"
        elif best["cs_falls"] <= 2 and best["cs_duty"] > 0.9:
            nss_verdict = "aliased_or_idle" if aliasing_risk else "cs_stuck_high"
        else:
            nss_verdict = "inconclusive"

        data_verdict = "chunk8188_artifact" if usb["chunk8188_autocorr"] > 0.5 else "ok_or_noisy"

        return {
            "pscl": pscl,
            "midi": midi,
            "stream": stream_info,
            "la_best": best,
            "la_snaps": len(la_frames),
            "usb": usb,
            "aliasing_risk": aliasing_risk,
            "nss_verdict": nss_verdict,
            "data_verdict": data_verdict,
            "chunk_agg_samples": CHUNK_AGG,
            "conclusion": _conclusion(nss_verdict, data_verdict, aliasing_risk, pscl),
        }
    finally:
        done.wait(timeout=90)
        th.join(timeout=10)
        la.relinquish_ownership()


def _conclusion(nss: str, data: str, alias: bool, pscl: int) -> str:
    parts: list[str] = []
    if nss == "pulsed_nss_ok":
        parts.append("SPI: pulsed NSS (короткие CS↓), группировки слов под одним CS нет.")
    elif nss == "aliased_or_idle" and alias:
        parts.append(
            f"SPI: при pscl={pscl} (~{385 if pscl==8 else 223} kS/s) LA API (~{204:.0f} kS/s) "
            "не разрешает CS-импульсы (алиасинг). В GUI Moku сигнал нормальный. "
            "Автопроверка NSS: --pscl 16."
        )
    elif nss == "cs_stuck_high":
        parts.append("SPI: CS постоянно HIGH во время стрима — проверьте PA11 и прошивку.")
    if data == "chunk8188_artifact":
        parts.append(
            f"Данные: периодический артефакт каждые {CHUNK_AGG} сэмплов "
            "(SPI_STREAM_AGG_MAX), не ошибка NSS — граница USB/DMA chunk."
        )
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku LA диагностика во время SPI stream")
    ap.add_argument("--moku", default=DEFAULT_MOKU)
    ap.add_argument("--pscl", type=int, default=16, help="8=боевой; 16/32 для LA")
    ap.add_argument("--midi", type=int, default=15)
    ap.add_argument("--stream-ch", type=int, default=2)
    ap.add_argument("--stream-n", type=int, default=200_000)
    ap.add_argument("--snaps", type=int, default=8)
    ap.add_argument("--compare-pscl8", action="store_true", help="сравнить pscl 8 и 16")
    args = ap.parse_args()

    if args.compare_pscl8:
        results = [
            run_diag(
                args.moku,
                pscl=8,
                midi=args.midi,
                stream_ch=args.stream_ch,
                stream_n=args.stream_n,
                snaps=args.snaps,
            ),
            run_diag(
                args.moku,
                pscl=16,
                midi=args.midi,
                stream_ch=args.stream_ch,
                stream_n=args.stream_n,
                snaps=args.snaps,
            ),
        ]
    else:
        results = [
            run_diag(
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
