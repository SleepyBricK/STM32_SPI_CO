#!/usr/bin/env python3
"""Minimal host echo test for STM32H743 USB vendor bulk."""

import argparse
import sys

import usb.core
import usb.util


VID = 0x0483
PID = 0x5741
EP_OUT = 0x01
EP_IN = 0x81


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--size", type=int, default=64)
    args = parser.parse_args()

    dev = usb.core.find(idVendor=args.vid, idProduct=args.pid)
    if dev is None:
        print(f"device {args.vid:04x}:{args.pid:04x} not found", file=sys.stderr)
        return 1

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]

    if dev.is_kernel_driver_active(intf.bInterfaceNumber):
        dev.detach_kernel_driver(intf.bInterfaceNumber)

    text = "".join(chr(ord("A") + (i % 26)) for i in range(args.size))
    payload = f"ECHO {text}\n".encode("ascii")
    expected = f"OK ECHO {text}".encode("ascii")
    dev.write(EP_OUT, payload, timeout=1000)
    got = bytes(dev.read(EP_IN, 512, timeout=1000)).rstrip()

    if got != expected:
        print(f"echo mismatch: sent={expected!r} got={got!r}", file=sys.stderr)
        return 2

    speed = getattr(dev, "speed", None)
    speed_text = f", speed={speed}" if speed is not None else ""
    print(f"OK echo {len(text)} bytes{speed_text}")
    usb.util.dispose_resources(dev)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
