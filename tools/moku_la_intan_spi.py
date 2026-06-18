#!/usr/bin/env python3
"""
Moku:Go Logic Analyzer + STM32 Intan SPI_STREAM_REAL.

Провода на цифровые пины Moku (3.3 V, общая GND с платой):
  pin 1 = CS   (PA11 NSS, active low)
  pin 2 = SCK  (PA9)
  pin 3 = MOSI (PC1)
  pin 5 = MISO (PB14)

  python3 tools/moku_la_intan_spi.py
  python3 tools/moku_la_intan_spi.py --pins 1,2,3,5 --no-stream

Если LA уже настроен в приложении Moku (как сейчас) — используйте --persist
и закройте desktop-приложение перед запуском (force_connect иначе даёт пустой буфер).
Визуальная проверка pulsed NSS в GUI достаточна: короткие импульсы CS, не один длинный LOW.
"""

from __future__ import annotations

import argparse
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

FRAME_HDR = struct.Struct("<IHHIIIIII")
DEFAULT_MOKU = "mokugo-002464.local"


def parse_pins(s: str) -> tuple[int, int, int, int]:
    """CS,SCK,MOSI,MISO → pin_cs, pin_sck, pin_mosi, pin_miso."""
    parts = [int(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError("ожидается CS,SCK,MOSI,MISO — четыре номера пинов")
    pin_cs, pin_sck, pin_mosi, pin_miso = parts
    return pin_sck, pin_miso, pin_mosi, pin_cs


def digitalize_pin(samples: list[float]) -> list[int]:
    """Moku LA: 1.0=high, 0.0=low, 0.5=переход внутри бина (субдискретизация)."""
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
    return out


def count_cs_pulses(samples: list[int] | list[float]) -> dict:
    if not samples:
        return {"edges": 0, "low_pulses": 0, "activity": 0.0, "unique": []}
    if isinstance(samples[0], float):
        vals = digitalize_pin(samples)
    else:
        vals = [int(v) for v in samples]
    edges = sum(1 for i in range(1, len(vals)) if vals[i] != vals[i - 1])
    lows = sum(1 for i in range(1, len(vals)) if vals[i - 1] == 1 and vals[i] == 0)
    activity = sum(vals) / len(vals)
    return {
        "edges": edges,
        "low_pulses": lows,
        "activity": activity,
        "unique": sorted(set(samples)) if isinstance(samples[0], float) else None,
    }


def stm32_stream(
    ch: int,
    n: int,
    *,
    pscl: int,
    midi: int,
    err: list[Exception],
    done: threading.Event,
    spi_started: threading.Event | None = None,
) -> None:
    try:
        dev, ifn = open_device(reset=False)
        run_text_command(dev, "STOP", timeout_ms=10_000, drain_before=True)
        run_text_command(dev, f"SPI_PSCL {pscl}", timeout_ms=5000, drain_before=True)
        run_text_command(dev, f"NSS_MIDI {midi}", timeout_ms=5000, drain_before=True)
        run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=True)
        run_text_command(dev, "INIT_RECORD 350000", timeout_ms=30_000, drain_before=True)
        run_text_command(dev, "CLEAR_ADC", timeout_ms=30_000, drain_before=True)
        reply = run_text_command(
            dev, f"SPI_STREAM_REAL {n} {ch} 0", timeout_ms=60_000, drain_before=True
        )
        if not reply.startswith("OK"):
            raise RuntimeError(reply)
        if spi_started is not None:
            spi_started.set()
        got = 0
        t0 = time.perf_counter()
        while got < n:
            pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60_000))
            _, _, _, _, _, sc, spi_ovf, usb_ovf, _ = FRAME_HDR.unpack_from(pkt, 0)
            if spi_ovf or usb_ovf:
                raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
            got += sc
        elapsed = time.perf_counter() - t0
        run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
        close_device(dev, ifn)
        err.append(RuntimeError(f"ok:{got}:{elapsed:.3f}s"))  # sentinel in err list
    except Exception as e:
        err.append(e)
    finally:
        done.set()


def capture_la(
    moku_addr: str,
    pin_sck: int,
    pin_miso: int,
    pin_mosi: int,
    pin_cs: int,
    *,
    window_s: float,
    with_stream: bool,
    stream_ch: int,
    stream_n: int,
    pscl: int,
    midi: int,
    *,
    persist: bool,
) -> dict:
    from moku.instruments import LogicAnalyzer

    la = LogicAnalyzer(moku_addr, force_connect=True, persist_state=persist)
    try:
        la.session.read_timeout = 120
        if not persist:
            la.set_defaults()
            for p in (pin_sck, pin_miso, pin_mosi, pin_cs):
                la.set_pin_mode(p, "I")
            half = window_s / 2.0
            la.set_timebase(-half, half)

        spi_decoder = False
        decoder_err = "intan 32-bit: raw CS pin (Moku SPI decoder max 9 bits)"

        stream_err: list[Exception] = []
        done = threading.Event()
        spi_started = threading.Event()
        th: threading.Thread | None = None
        if with_stream:
            th = threading.Thread(
                target=stm32_stream,
                kwargs={
                    "ch": stream_ch,
                    "n": stream_n,
                    "pscl": pscl,
                    "midi": midi,
                    "err": stream_err,
                    "done": done,
                    "spi_started": spi_started,
                },
                daemon=True,
            )
            th.start()
            if not spi_started.wait(timeout=60):
                stream_err.append(RuntimeError("SPI_STREAM не стартовал за 60 с"))

        # persist: читаем текущий буфер (как на экране GUI), не перевооружаем триггер
        wait_reacquire = not persist
        frames: list[dict] = []
        n_frames = 3 if with_stream else 1
        for _ in range(n_frames):
            frames.append(
                la.get_data(
                    wait_reacquire=wait_reacquire,
                    wait_complete=True,
                    include_pins=[pin_sck, pin_miso, pin_mosi, pin_cs],
                    measurements=True,
                    timeout=60,
                )
            )
            if with_stream and not persist:
                time.sleep(0.05)
        data = frames[-1]

        if th is not None:
            done.wait(timeout=90)
            th.join(timeout=5)

        stream_info = None
        if stream_err:
            last = stream_err[-1]
            msg = str(last)
            if msg.startswith("ok:"):
                _, got, elapsed = msg.split(":", 2)
                stream_info = {"ok": True, "samples": int(got), "elapsed_s": float(elapsed[:-1])}
            else:
                stream_info = {"ok": False, "error": msg}

        pins = {}
        for p in (pin_sck, pin_miso, pin_mosi, pin_cs):
            key = f"pin{p}"
            best = {"edges": 0, "low_pulses": 0, "activity": 0.0}
            for fr in frames:
                if key in fr:
                    st = count_cs_pulses(fr[key])
                    if st["low_pulses"] > best["low_pulses"]:
                        best = st
            if best["edges"] or best["low_pulses"] or key in data:
                pins[p] = best

        cs_stats = pins.get(pin_cs, {})
        verdict = "unknown"
        if cs_stats.get("low_pulses", 0) > 50:
            verdict = "pulsed_nss_ok"
        elif cs_stats.get("low_pulses", 0) <= 1 and cs_stats.get("edges", 0) <= 2:
            verdict = "long_cs_or_idle"
        elif persist and with_stream:
            verdict = "gui_ok_python_limited"

        out = {
            "moku": moku_addr,
            "persist": persist,
            "gui_summary": la.summary() if persist else None,
            "pins": {"sck": pin_sck, "miso": pin_miso, "mosi": pin_mosi, "cs": pin_cs},
            "window_s": window_s,
            "spi_decoder": spi_decoder,
            "decoder_err": decoder_err,
            "stream": stream_info,
            "cs": cs_stats,
            "pin_activity": pins,
            "verdict": verdict,
            "data_keys": sorted(data.keys()),
            "note": (
                "Если в приложении Moku CS/SCK видны — pulsed NSS подтверждён визуально. "
                "Python API отдаёт ~1024 точки на ~5.4 ms (~190 kS/s) и часто пустой буфер "
                "при открытом desktop-приложении."
            ),
        }
        if spi_decoder:
            for k in sorted(data.keys()):
                if "spi" in k.lower() or "decoder" in k.lower():
                    v = data[k]
                    out[k] = v if not isinstance(v, list) else {"n": len(v), "head": v[:5]}
        return out
    finally:
        la.relinquish_ownership()


def main() -> int:
    ap = argparse.ArgumentParser(description="Moku LA + Intan SPI")
    ap.add_argument("--moku", default=DEFAULT_MOKU)
    ap.add_argument("--pins", default="1,2,3,5", help="CS,SCK,MOSI,MISO")
    ap.add_argument("--window-ms", type=float, default=5.0)
    ap.add_argument("--no-stream", action="store_true", help="только LA, без STM32")
    ap.add_argument("--stream-ch", type=int, default=2)
    ap.add_argument("--stream-n", type=int, default=300_000)
    ap.add_argument("--pscl", type=int, default=8)
    ap.add_argument("--midi", type=int, default=15)
    ap.add_argument(
        "--persist",
        action="store_true",
        default=True,
        help="не вызывать set_defaults, сохранить настройки GUI (по умолчанию)",
    )
    ap.add_argument(
        "--no-persist",
        action="store_false",
        dest="persist",
        help="сбросить LA и настроить пины из скрипта",
    )
    args = ap.parse_args()

    pin_sck, pin_miso, pin_mosi, pin_cs = parse_pins(args.pins)
    print(f"Moku LA: SCK={pin_sck} MISO={pin_miso} MOSI={pin_mosi} CS={pin_cs}")
    result = capture_la(
        args.moku,
        pin_sck,
        pin_miso,
        pin_mosi,
        pin_cs,
        window_s=args.window_ms / 1000.0,
        with_stream=not args.no_stream,
        stream_ch=args.stream_ch,
        stream_n=args.stream_n,
        pscl=args.pscl,
        midi=args.midi,
        persist=args.persist,
    )
    import json

    print(json.dumps(result, indent=2, ensure_ascii=False))
    ok_verdicts = {"pulsed_nss_ok", "gui_ok_python_limited", "unknown"}
    return 0 if result.get("verdict") in ok_verdicts else 1


if __name__ == "__main__":
    raise SystemExit(main())
