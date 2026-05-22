#!/usr/bin/env python3
"""Measure STM32H743 Intan USB bulk sample streaming throughput."""

import argparse
import struct
import sys
import time

import usb.core
import usb.util


VID = 0x0483
PID = 0x5741
EP_OUT = 0x01
EP_IN = 0x81


def open_device(vid: int, pid: int):
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"device {vid:04x}:{pid:04x} not found")

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    if sys.platform.startswith("linux") and dev.is_kernel_driver_active(intf.bInterfaceNumber):
        dev.detach_kernel_driver(intf.bInterfaceNumber)
    return dev


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
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--samples", type=int, default=50000)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--flags", type=lambda x: int(x, 0), default=0)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--dump-first", type=int, default=8)
    args = parser.parse_args()

    if args.samples <= 0:
        print("samples must be > 0", file=sys.stderr)
        return 2

    try:
        dev = open_device(args.vid, args.pid)
        cmd = f"STREAM {args.samples} {args.channel} {args.flags}\n".encode("ascii")
        t0 = time.perf_counter()
        dev.write(EP_OUT, cmd, timeout=args.timeout_ms)
        payload = read_exact(dev, args.samples * 2, args.timeout_ms)
        elapsed = time.perf_counter() - t0

        first_count = min(args.dump_first, args.samples)
        first = struct.unpack_from("<" + "H" * first_count, payload, 0) if first_count else ()
        print(f"samples={args.samples} bytes={len(payload)} elapsed={elapsed:.6f}s")
        print(f"ksps_total={args.samples / elapsed / 1000.0:.3f}")
        print(f"throughput={len(payload) / elapsed / 1e6:.3f} MB/s")
        print("first=" + " ".join(f"0x{x:04X}" for x in first))
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
