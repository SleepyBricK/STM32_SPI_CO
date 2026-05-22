#!/usr/bin/env python3
"""Send one STM32H743 Intan USB bulk command and print the result."""

import argparse
import sys

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="*", default=["ID"], help="USB command, e.g. ID, READ 255, CONVERT 0")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    args = parser.parse_args()

    command = " ".join(args.command).strip()
    if not command:
        command = "ID"

    try:
        dev = open_device(args.vid, args.pid)
        payload = (command + "\n").encode("ascii")
        dev.write(EP_OUT, payload, timeout=args.timeout_ms)
        data = bytes(dev.read(EP_IN, 512, timeout=args.timeout_ms))
        print(data.rstrip(b"\0").decode("ascii", errors="replace").rstrip())
        usb.util.dispose_resources(dev)
    except Exception as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
