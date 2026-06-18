#!/usr/bin/env python3
"""
DSLogic (DreamSourceLab) + STM32 Intan SPI — захват и разбор через sigrok-cli.

Провода (дефолт — поменяйте --clk/--miso/--mosi/--cs под свою разводку LA):
  D0 = SCK  (PA9)
  D1 = MISO (PB14)
  D2 = MOSI (PC1)
  D3 = NSS  (PA11)   активный низкий, pulsed между 32-bit кадрами

Требования (один раз на Mac):
  brew install sigrok-cli libsigrokdecode
  # firmware DSLogic (если --scan не видит устройство):
  FW=/opt/homebrew/share/sigrok-firmware
  curl -fsSL .../DSLogic.fw -o $FW/dreamsourcelab-dslogic-fx2.fw
  (см. sigrok-util/firmware/dreamsourcelab-dslogic/)

Перед захватом закройте DSView — иначе USB занят.

Примеры:
  python3 tools/dslogic_intan_spi.py --scan
  python3 tools/dslogic_intan_spi.py --capture-only -o tools/dslogic_intan.sr
  python3 tools/dslogic_intan_spi.py --with-stream --samples 500k -o tools/dslogic_intan.sr
  python3 tools/dslogic_intan_spi.py -i tools/dslogic_intan.sr --analyze
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

SIGROK_CANDIDATES = (
    "/opt/homebrew/bin/sigrok-cli",
    "/usr/local/bin/sigrok-cli",
    "sigrok-cli",
)
DRIVER = "dreamsourcelab-dslogic"
FRAME_HDR = struct.Struct("<IHHIIIIII")


def find_sigrok() -> str:
    for p in SIGROK_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("sigrok-cli")
    if found:
        return found
    raise SystemExit("sigrok-cli не найден. Установите: brew install sigrok-cli")


def run_sigrok(sigrok: str, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = [sigrok, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# PID 0x0029 и др. — «Instrument v2», не поддерживаются libsigrok 0.5.x (нужен DSView).
DSLOGIC_UNSUPPORTED_PIDS = {
    0x0029: "DSLogic U2Basic",
    0x002A: "DSLogic U3Pro16",
    0x002C: "DSLogic U3Pro32",
    0x002D: "DSLogic U2Pro16",
    0x0030: "DSLogic Plus (pgl12)",
    0x0031: "DSLogic U2Basic (pgl12)",
    0x0034: "DSLogic Plus (pgl12-2)",
    0x0035: "DSLogic U2Basic (pgl12-2)",
}


def detect_dslogic_usb() -> tuple[int, int, str] | None:
    import usb.core

    for dev in usb.core.find(find_all=True):
        if dev.idVendor != 0x2A0E:
            continue
        try:
            name = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
        except Exception:
            name = ""
        return dev.idVendor, dev.idProduct, name
    return None


def scan_devices(sigrok: str) -> None:
    usb_dev = detect_dslogic_usb()
    if usb_dev is not None:
        vid, pid, name = usb_dev
        model = DSLOGIC_UNSUPPORTED_PIDS.get(pid)
        print(f"USB: {vid:04x}:{pid:04x}  {name}")
        if model:
            print(
                f"\n{model}: libsigrok/sigrok-cli не поддерживает этот PID.\n"
                "Захват — через DSView (https://www.dreamsourcelab.com/download/).\n"
                "Экспорт CSV/VCD из DSView → разбор вручную или конвертация в .sr.\n"
                "См. провода: D0=SCK D1=MISO D2=MOSI D3=NSS (PA11)."
            )
            return

    r = run_sigrok(sigrok, "--scan")
    print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, file=sys.stderr, end="")
    if "dreamsourcelab" not in r.stdout.lower() and "dslogic" not in r.stdout.lower():
        print(
            "\nDSLogic не найден. Проверьте USB, закройте DSView, прошивку sigrok-firmware.",
            file=sys.stderr,
        )


def capture_raw(
    sigrok: str,
    out: Path,
    *,
    channels: str,
    rate: str,
    samples: int,
    voltage: str,
    decode_spi: bool,
    clk: int,
    miso: int,
    mosi: int,
    cs: int,
) -> None:
    args = [
        "-d",
        DRIVER,
        "-C",
        channels,
        "-c",
        f"samplerate={rate}:voltage_threshold={voltage}",
        "--samples",
        str(samples),
        "-o",
        str(out),
    ]
    if decode_spi:
        args += [
            "-P",
            (
                f"spi:clk={clk}:miso={miso}:mosi={mosi}:cs={cs}"
                f":cpol=0:cpha=0:wordsize=32"
            ),
        ]
    print("sigrok:", " ".join(args))
    r = run_sigrok(sigrok, *args, timeout=max(120.0, samples / 1_000_000 * 10))
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"sigrok capture failed ({r.returncode})")
    if r.stdout:
        print(r.stdout)
    print(f"Saved: {out}")


def stm32_stream_drain(
    ch: int = 2,
    n: int = 4000,
    *,
    ksps: int = 350_000,
    pscl: int = 8,
    midi: int = 15,
    reset_usb: bool = False,
) -> None:
    """SPI_STREAM_REAL + чтение RHS1 bulk (обязательно, иначе usb_ovf и зависание USB)."""
    dev, ifn = open_device(reset=reset_usb)
    try:
        run_text_command(dev, "STOP", timeout_ms=10_000, drain_before=True)
        run_text_command(dev, f"SPI_PSCL {pscl}", timeout_ms=5000, drain_before=True)
        run_text_command(dev, f"NSS_MIDI {midi}", timeout_ms=5000, drain_before=True)
        run_text_command(dev, "WRITE 42 0 1 0", timeout_ms=5000, drain_before=True)
        run_text_command(dev, f"INIT_RECORD {ksps}", timeout_ms=30_000, drain_before=True)
        run_text_command(dev, "CLEAR_ADC", timeout_ms=30_000, drain_before=True)
        reply = run_text_command(
            dev, f"SPI_STREAM_REAL {n} {ch} 0", timeout_ms=60_000, drain_before=True
        )
        if not reply.startswith("OK"):
            raise RuntimeError(reply)

        got = 0
        t0 = time.perf_counter()
        print(f"stream ch{ch} n={n} — читайте SPI в DSView (~{n / ksps:.1f} s)", flush=True)
        while got < n:
            pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=60_000))
            if len(pkt) < FRAME_HDR.size:
                raise RuntimeError("short frame")
            magic, version, _, _, _, sc, spi_ovf, usb_ovf, _ = FRAME_HDR.unpack_from(pkt, 0)
            if magic != 0x52485331 or version != 1:
                raise RuntimeError("bad frame header")
            if spi_ovf or usb_ovf:
                raise RuntimeError(f"overflow spi={spi_ovf} usb={usb_ovf}")
            got += sc
            if got % 200_000 < sc:
                print(f"  {got}/{n} samples", flush=True)

        elapsed = time.perf_counter() - t0
        ksps_act = got / elapsed if elapsed > 0 else 0.0
        run_text_command(dev, "STOP", timeout_ms=10_000, drain_before=False)
        print(f"STM32 stream OK: {got} samples, {elapsed:.2f}s, {ksps_act/1000:.1f} kS/s")
    finally:
        close_device(dev, ifn)


def stm32_short_stream(ch: int = 2, n: int = 4000) -> None:
    stm32_stream_drain(ch, n)


def capture_with_stream(
    sigrok: str,
    out: Path,
    *,
    channels: str,
    rate: str,
    samples: int,
    voltage: str,
    stream_ch: int,
    stream_n: int,
    delay_s: float,
    decode_spi: bool,
    clk: int,
    miso: int,
    mosi: int,
    cs: int,
) -> None:
    err: list[Exception] = []

    def worker() -> None:
        try:
            time.sleep(delay_s)
            stm32_short_stream(stream_ch, stream_n)
        except Exception as e:
            err.append(e)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    capture_raw(
        sigrok,
        out,
        channels=channels,
        rate=rate,
        samples=samples,
        voltage=voltage,
        decode_spi=decode_spi,
        clk=clk,
        miso=miso,
        mosi=mosi,
        cs=cs,
    )
    th.join(timeout=5.0)
    if err:
        raise err[0]


def analyze_sr(sigrok: str, path: Path, clk: int, miso: int, mosi: int, cs: int) -> None:
    if not path.is_file():
        raise SystemExit(f"Нет файла: {path}")

    # Декод SPI → текст
    r = run_sigrok(
        sigrok,
        "-i",
        str(path),
        "-P",
        f"spi:clk={clk}:miso={miso}:mosi={mosi}:cs={cs}:cpol=0:cpha=0:wordsize=32",
        timeout=120.0,
    )
    text = r.stdout + r.stderr
    print("--- SPI decode (фрагмент) ---")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[:40]:
        print(ln)
    if len(lines) > 40:
        print(f"... ещё {len(lines) - 40} строк")

    # Подсчёт MOSI-слов (строки с MOSI:)
    mosi_words = [ln for ln in lines if re.search(r"\bMOSI\b", ln, re.I)]
    print(f"\nСтрок декодера с MOSI: {len(mosi_words)}")

    # CSV для CS timing (logic channels)
    r2 = run_sigrok(
        sigrok,
        "-i",
        str(path),
        "-O",
        "csv",
        "-C",
        f"{clk},{cs}",
        timeout=60.0,
    )
    cs_edges = 0
    prev = None
    for ln in r2.stdout.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 3:
            continue
        try:
            val = int(float(parts[2 + cs]))  # channel column order: sample, time, ch0, ch1...
        except (ValueError, IndexError):
            continue
        if prev is not None and val != prev:
            cs_edges += 1
        prev = val
    print(f"Переключений CS (канал D{cs}): {cs_edges}")
    print(
        "Ожидание pulsed NSS: CS↑ между каждым 32-bit кадром → много коротких импульсов, "
        "не один длинный CS на весь burst."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="DSLogic + Intan SPI (sigrok)")
    ap.add_argument("--scan", action="store_true", help="показать USB-устройства sigrok")
    ap.add_argument("--stream-only", action="store_true", help="только STM32 SPI_STREAM_REAL + drain bulk")
    ap.add_argument("--wait-device", type=float, default=0.0, help="ждать 0483:5741, сек (0=не ждать)")
    ap.add_argument("--capture-only", action="store_true", help="только LA, без STM32")
    ap.add_argument("--with-stream", action="store_true", help="LA + короткий SPI_STREAM_REAL")
    ap.add_argument("-o", "--output", type=Path, default=Path("tools/dslogic_intan.sr"))
    ap.add_argument("-i", "--input", type=Path, help="анализ .sr")
    ap.add_argument("--analyze", action="store_true", help="разобрать -i")
    ap.add_argument("--channels", default="0,1,2,3", help="каналы LA (D0..D3)")
    ap.add_argument("--rate", default="10M", help="samplerate, напр. 10M или 25M")
    ap.add_argument("--samples", type=int, default=2_000_000, help="число сэмплов LA")
    ap.add_argument("--voltage", default="1.5-1.5", help="порог IO, 3.3V: 1.5-1.5")
    ap.add_argument("--clk", type=int, default=0)
    ap.add_argument("--miso", type=int, default=1)
    ap.add_argument("--mosi", type=int, default=2)
    ap.add_argument("--cs", type=int, default=3)
    ap.add_argument("--stream-ch", type=int, default=2)
    ap.add_argument("--stream-n", type=int, default=4000)
    ap.add_argument("--stream-delay", type=float, default=0.15, help="с задержкой до stream")
    ap.add_argument("--no-decode", action="store_true")
    args = ap.parse_args()

    sigrok = find_sigrok()

    if args.scan:
        scan_devices(sigrok)
        return 0

    if args.stream_only:
        deadline = time.monotonic() + args.wait_device if args.wait_device > 0 else None
        while True:
            try:
                import usb.core

                if usb.core.find(idVendor=0x0483, idProduct=0x5741) is None:
                    raise OSError("0483:5741 not found")
                stm32_stream_drain(
                    args.stream_ch,
                    args.stream_n,
                    reset_usb=args.wait_device > 0,
                )
                return 0
            except OSError as e:
                if deadline is None or time.monotonic() >= deadline:
                    print(f"ERR {e}", file=sys.stderr)
                    return 1
                time.sleep(0.5)

    if args.input and args.analyze:
        analyze_sr(sigrok, args.input, args.clk, args.miso, args.mosi, args.cs)
        return 0

    if args.capture_only or args.with_stream:
        if args.with_stream:
            capture_with_stream(
                sigrok,
                args.output,
                channels=args.channels,
                rate=args.rate,
                samples=args.samples,
                voltage=args.voltage,
                stream_ch=args.stream_ch,
                stream_n=args.stream_n,
                delay_s=args.stream_delay,
                decode_spi=not args.no_decode,
                clk=args.clk,
                miso=args.miso,
                mosi=args.mosi,
                cs=args.cs,
            )
        else:
            capture_raw(
                sigrok,
                args.output,
                channels=args.channels,
                rate=args.rate,
                samples=args.samples,
                voltage=args.voltage,
                decode_spi=not args.no_decode,
                clk=args.clk,
                miso=args.miso,
                mosi=args.mosi,
                cs=args.cs,
            )
        if args.analyze:
            analyze_sr(sigrok, args.output, args.clk, args.miso, args.mosi, args.cs)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
