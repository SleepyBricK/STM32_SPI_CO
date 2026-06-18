#!/usr/bin/env python3
"""
Moku LA: максимальная дискретизация SPI во время SPI_STREAM_REAL.

Moku:Go: get_data всегда 1024 точки; частота = 1024 / (t2-t1).
  t1,t2 = ±2.048 µs  →  ~125 MSa/s, окно ~8.2 µs (достаточно для SCK 25 MHz).
  t1,t2 = ±2.5 ms    →  ~200 kSa/s (алиасинг на pscl=8).

High-res: save_high_res_buffer → ~524k точек @ 125 MSa/s → ~4.2 ms захвата.

Требуется: mokucli (brew/install utilities), закрыть desktop Moku app.

  python3 tools/moku_la_spi_hires.py
  python3 tools/moku_la_spi_hires.py --pscl 8 --high-res --keep-files
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

FRAME_HDR = struct.Struct("<IHHIIIIII")
DEFAULT_MOKU = "mokugo-002464.local"
# Moku:Go: минимальное окно ≈ 8.184 µs → 125.12 MSa/s при 1024 точках
MAX_FS_HALF_S = 2.048e-6
SCK_HZ_BASE = 200_000_000
SLOT_SCK_CYCLES = 42
PIN_MAP = {"cs": 1, "sck": 2, "mosi": 3, "miso": 5}


def digitalize(arr: np.ndarray) -> np.ndarray:
    return (np.asarray(arr) >= 0.5).astype(np.int8)


def cs_fall_rise(cs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = np.diff(cs)
    return np.where(d == -1)[0], np.where(d == 1)[0]


def analyze_spi_trace(
    t: np.ndarray,
    cs: np.ndarray,
    sck: np.ndarray,
    mosi: np.ndarray,
    miso: np.ndarray,
    *,
    pscl: int,
    wall_ksps: float,
    label: str,
) -> dict:
    span = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    fs = len(t) / span if span > 0 else 0.0
    fall, rise = cs_fall_rise(cs)
    widths_us: list[float] = []
    sck_in_low: list[int] = []
    mosi_in_low: list[int] = []
    miso_in_low: list[int] = []
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

    for f in fall:
        r = rise[rise > f]
        if len(r) == 0:
            continue
        a, b = int(f), int(r[0])
        widths_us.append(float((t[b - 1] - t[a]) * 1e6) if b > a else 0.0)
        if b - a >= 2:
            sck_in_low.append(int(np.sum(np.diff(sck[a:b]) != 0)))
            mosi_in_low.append(int(np.sum(np.diff(mosi[a:b]) != 0)))
            miso_in_low.append(int(np.sum(np.diff(miso[a:b]) != 0)))

    sck_high_windows = []
    i = 0
    sck_on_high = 0
    while i < len(cs):
        if cs[i] == 1:
            j = i
            while j < len(cs) and cs[j] == 1:
                j += 1
            if j - i >= 2:
                sck_on_high += int(np.sum(np.diff(sck[i:j]) != 0))
            i = j
        else:
            i += 1

    sck_mhz = SCK_HZ_BASE / pscl / 1e6
    cs_rate_khz = len(fall) / span / 1000 if span > 0 else 0.0
    sck_per_cs = float(np.median(sck_in_low)) if sck_in_low else 0.0

    cs_low_us_max = float(np.max(widths_us)) if widths_us else 0.0
    cs_low_us_med = float(np.median(widths_us)) if widths_us else 0.0

    checks = {
        "cs_visible": "OK" if len(fall) >= 3 else "FAIL",
        "cs_short_pulses": "OK" if cs_low_us_med < 2.0 and cs_low_us_max < 5.0 else "FAIL_LONG",
        "sck_only_in_cs": "OK" if sck_on_high <= max(2, len(fall) // 10) else "FAIL",
        "mosi_in_cs": "OK" if (np.mean([x > 0 for x in mosi_in_low]) if mosi_in_low else 0) > 0.3 else "WEAK",
        "miso_in_cs": "OK" if (np.mean([x > 0 for x in miso_in_low]) if miso_in_low else 0) > 0.3 else "WEAK",
        "sck_edges_per_cs": "OK" if 20 <= sck_per_cs <= 80 else "WARN",
    }
    fails = [k for k, v in checks.items() if v.startswith("FAIL")]

    return {
        "label": label,
        "span_us": span * 1e6,
        "fs_msps": fs / 1e6,
        "n_samples": len(t),
        "pscl": pscl,
        "sck_mhz": sck_mhz,
        "wall_ksps": wall_ksps,
        "cs_falls": int(len(fall)),
        "cs_rate_khz": cs_rate_khz,
        "cs_vs_stream": cs_rate_khz / wall_ksps if wall_ksps > 0 else 0.0,
        "cs_low_us": {
            "min": float(np.min(widths_us)) if widths_us else None,
            "med": float(np.median(widths_us)) if widths_us else None,
            "max": float(np.max(widths_us)) if widths_us else None,
        },
        "cs_low_samples_max": max(low_runs) if low_runs else 0,
        "sck_edges_total": int(np.sum(np.diff(sck) != 0)),
        "sck_edges_per_cs_med": sck_per_cs,
        "sck_edges_on_cs_high": sck_on_high,
        "mosi_edges_total": int(np.sum(np.diff(mosi) != 0)),
        "miso_edges_total": int(np.sum(np.diff(miso) != 0)),
        "slot_sck_cycles_expected": SLOT_SCK_CYCLES,
        "checks": checks,
        "verdict": "spi_ok" if not fails else "spi_bad",
    }


def setup_la(la, *, half_s: float) -> None:
    la.set_defaults()
    for p in PIN_MAP.values():
        la.set_pin_mode(p, "I")
    la.set_timebase(-half_s, half_s)
    la.set_trigger(pins=[{"pin": PIN_MAP["cs"], "edge": "Falling"}], mode="Auto")


def pins_from_get_data(d: dict) -> tuple[np.ndarray, ...]:
    t = np.array(d["time"])
    return (
        t,
        digitalize(d[f"pin{PIN_MAP['cs']}"]),
        digitalize(d[f"pin{PIN_MAP['sck']}"]),
        digitalize(d[f"pin{PIN_MAP['mosi']}"]),
        digitalize(d[f"pin{PIN_MAP['miso']}"]),
    )


def pins_from_hires_npy(arr: np.ndarray) -> tuple[np.ndarray, ...]:
    t = arr["Time (s)"]
    return (
        t,
        digitalize(arr["Pin 1"]),
        digitalize(arr["Pin 2"]),
        digitalize(arr["Pin 3"]),
        digitalize(arr["Pin 5"]),
    )


def best_activity_slice(cs: np.ndarray, window: int = 65_536) -> slice:
    """Окно с максимальным числом CS↓ (для high-res буфера)."""
    if len(cs) <= window:
        return slice(0, len(cs))
    step = max(window // 8, 1)
    best_start = 0
    best_falls = -1
    for start in range(0, len(cs) - window, step):
        sub = cs[start : start + window]
        n = int(np.sum(np.diff(sub) == -1))
        if n > best_falls:
            best_falls = n
            best_start = start
    return slice(best_start, best_start + window)


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


def convert_li_to_npy(li_path: str) -> str:
    subprocess.run(
        ["mokucli", "convert", "--format=npy", li_path],
        check=True,
        capture_output=True,
        text=True,
    )
    npy = li_path.replace(".li", ".npy")
    if not os.path.isfile(npy):
        raise FileNotFoundError(f"mokucli не создал {npy}")
    return npy


def run_capture(
    moku: str,
    *,
    pscl: int,
    midi: int,
    stream_ch: int,
    stream_n: int,
    half_s: float,
    high_res: bool,
    keep_files: bool,
    out_dir: str | None,
) -> dict:
    from moku.instruments import LogicAnalyzer

    if high_res and not shutil_which("mokucli"):
        raise RuntimeError("mokucli не найден — установите Moku utilities для --high-res")

    spi_started = threading.Event()
    done = threading.Event()
    stream_info: dict = {}
    tmpdir = out_dir or tempfile.mkdtemp(prefix="moku_spi_hires_")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)

    la = LogicAnalyzer(moku, force_connect=True, persist_state=False)
    la.session.read_timeout = 180
    try:
        setup_la(la, half_s=half_s)
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
            stream_info["error"] = "SPI_STREAM не стартовал"

        time.sleep(0.05)

        hires = None
        files: dict[str, str] = {}
        if high_res:
            resp = la.save_high_res_buffer("intan_spi_dma", timeout=60)
            fname = resp["file_name"]
            li_path = str(Path(tmpdir) / fname)
            la.download("persist", fname, li_path)
            files["li"] = li_path
            npy_path = convert_li_to_npy(li_path)
            files["npy"] = npy_path
            arr = np.load(npy_path)
            ht, hcs, hsck, hmosi, hmiso = pins_from_hires_npy(arr)
            sl = best_activity_slice(hcs)
            hires = analyze_spi_trace(
                ht[sl], hcs[sl], hsck[sl], hmosi[sl], hmiso[sl],
                pscl=pscl,
                wall_ksps=stream_info.get("wall_ksps", 0.0),
                label="high_res_buffer",
            )
            hires["n_samples_total"] = len(ht)
            hires["slice_start"] = int(sl.start)
            hires["slice_len"] = int(sl.stop - sl.start)
            if not keep_files:
                for p in files.values():
                    try:
                        os.remove(p)
                        txt = p.replace(".npy", ".txt")
                        if os.path.isfile(txt):
                            os.remove(txt)
                    except OSError:
                        pass

        d = la.get_data(
            wait_reacquire=True,
            wait_complete=True,
            include_pins=list(PIN_MAP.values()),
            timeout=60,
        )
        t, cs, sck, mosi, miso = pins_from_get_data(d)
        frame = analyze_spi_trace(
            t, cs, sck, mosi, miso,
            pscl=pscl,
            wall_ksps=stream_info.get("wall_ksps", 0.0),
            label="frame_125msps" if half_s <= MAX_FS_HALF_S * 1.01 else "frame_slow",
        )

        return {
            "moku": moku,
            "half_window_us": half_s * 2e6,
            "stream": stream_info,
            "frame": frame,
            "high_res": hires,
            "files": files if keep_files else {},
            "work_dir": tmpdir if keep_files else None,
            "note": (
                "125 MSa/s: set_timebase(±2.048µs). High-res ~524k pts @ 125 MSa/s ≈ 4.2 ms. "
                "sck_edges_per_cs_med ≈ 32 (данные) … 42 (слот с MIDI)."
            ),
        }
    finally:
        done.wait(timeout=120)
        th.join(timeout=10)
        la.relinquish_ownership()


def shutil_which(cmd: str) -> str | None:
    from shutil import which
    return which(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku LA max sample rate SPI capture")
    ap.add_argument("--moku", default=DEFAULT_MOKU)
    ap.add_argument("--pscl", type=int, default=8)
    ap.add_argument("--midi", type=int, default=15)
    ap.add_argument("--stream-ch", type=int, default=2)
    ap.add_argument("--stream-n", type=int, default=300_000)
    ap.add_argument(
        "--half-us",
        type=float,
        default=MAX_FS_HALF_S * 1e6,
        help="полуокно триггера в µs (2.048 = 125 MSa/s на Moku:Go)",
    )
    ap.add_argument("--slow", action="store_true", help="окно ±2.5 ms (~200 kSa/s, для сравнения)")
    ap.add_argument("--high-res", action="store_true", default=True, help="save_high_res_buffer (по умолчанию)")
    ap.add_argument("--no-high-res", action="store_false", dest="high_res")
    ap.add_argument("--keep-files", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    half_s = 2.5e-3 if args.slow else args.half_us * 1e-6
    result = run_capture(
        args.moku,
        pscl=args.pscl,
        midi=args.midi,
        stream_ch=args.stream_ch,
        stream_n=args.stream_n,
        half_s=half_s,
        high_res=args.high_res,
        keep_files=args.keep_files,
        out_dir=args.out_dir,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
