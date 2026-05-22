#!/usr/bin/env python3
"""Host tests for firmware-ulpi (USB3300 ULPI, PID 5742)."""

import argparse
import struct
import sys
import time

import usb.core
import usb.util

VID = 0x0483
PID = 0x5742
EP_OUT = 0x01
EP_IN = 0x81


def list_usb_devices() -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for dev in usb.core.find(find_all=True):
        try:
            mfg = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else ""
        except Exception:
            mfg = "?"
        try:
            prod = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
        except Exception:
            prod = "?"
        out.append((dev.idVendor, dev.idProduct, f"{mfg} / {prod}".strip(" /")))
    return out


def open_device(vid: int, pid: int):
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        visible = list_usb_devices()
        hint = "pyusb sees 0 devices — on macOS: brew install libusb; on Linux: udev/group plugdev"
        if visible:
            lines = [f"  {v:04x}:{p:04x}  {label}" for v, p, label in visible]
            hint = "visible USB devices:\n" + "\n".join(lines)
        raise RuntimeError(f"device {vid:04x}:{pid:04x} not found\n{hint}")

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    if sys.platform.startswith("linux") and dev.is_kernel_driver_active(intf.bInterfaceNumber):
        dev.detach_kernel_driver(intf.bInterfaceNumber)
    return dev


def cmd_line(dev, text: str, timeout_ms: int = 1000) -> bytes:
    dev.write(EP_OUT, text.encode("ascii"), timeout=timeout_ms)
    return bytes(dev.read(EP_IN, 512, timeout=timeout_ms)).rstrip()


def read_exact(dev, nbytes: int, timeout_ms: int) -> bytes:
    chunks = []
    remaining = nbytes
    while remaining > 0:
        req = min(remaining, 16 * 1024)
        data = bytes(dev.read(EP_IN, req, timeout=timeout_ms))
        if not data:
            raise RuntimeError("short USB read")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="ULPI firmware USB smoke/bench test")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--echo-size", type=int, default=64)
    parser.add_argument("--stream", type=int, default=0, help="if >0, run STREAM benchmark")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args()

    try:
        dev = open_device(args.vid, args.pid)

        pong = cmd_line(dev, "PING\n", args.timeout_ms).decode("ascii")
        if pong != "OK PONG":
            print(f"PING failed: {pong!r}", file=sys.stderr)
            return 2

        text = "".join(chr(ord("A") + (i % 26)) for i in range(args.echo_size))
        echo = cmd_line(dev, f"ECHO {text}\n", args.timeout_ms).decode("ascii")
        expected = f"OK ECHO {text}"
        if echo != expected:
            print(f"ECHO failed: got {echo!r}", file=sys.stderr)
            return 3

        status = cmd_line(dev, "STATUS\n", args.timeout_ms).decode("ascii")
        print(f"PING OK, ECHO {args.echo_size} bytes OK")
        print(f"STATUS: {status}")

        if args.stream > 0:
            t0 = time.perf_counter()
            dev.write(EP_OUT, f"STREAM {args.stream}\n".encode("ascii"), timeout=args.timeout_ms)
            payload = read_exact(dev, args.stream * 2, args.timeout_ms)
            elapsed = time.perf_counter() - t0
            first = struct.unpack_from("<8H", payload, 0)
            print(f"STREAM samples={args.stream} bytes={len(payload)} elapsed={elapsed:.6f}s")
            print(f"throughput={len(payload) / elapsed / 1e6:.3f} MB/s")
            print("first=" + " ".join(f"0x{x:04X}" for x in first))
            if first != tuple(range(8)):
                print("WARN ramp pattern mismatch at start", file=sys.stderr)

        try:
            print(f"pyusb speed={dev.speed}")
        except Exception:
            pass

        usb.util.dispose_resources(dev)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
