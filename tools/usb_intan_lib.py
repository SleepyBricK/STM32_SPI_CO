"""Shared PyUSB helpers for STM32H743 Intan vendor bulk (Linux-friendly)."""

from __future__ import annotations

import sys
import time

import usb.core
import usb.util

VID = 0x0483
PID = 0x5741
EP_OUT = 0x01
EP_IN = 0x81


def find_device(vid: int = VID, pid: int = PID) -> usb.core.Device:
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"device {vid:04x}:{pid:04x} not found")
    return dev


def bus_reset(vid: int = VID, pid: int = PID, settle_s: float = 1.5) -> usb.core.Device:
    """Reset USB device; clears firmware stuck after interrupted STREAM."""
    dev = find_device(vid, pid)
    try:
        dev.reset()
    except usb.core.USBError:
        pass
    time.sleep(settle_s)
    dev = find_device(vid, pid)
    return dev


def drain_in(dev: usb.core.Device, timeout_ms: int = 50) -> int:
    """Discard stale IN data from a previous partial transfer."""
    total = 0
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 512, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            break
        except usb.core.USBError:
            break
        if not chunk:
            break
        total += len(chunk)
    return total


def open_device(
    vid: int = VID,
    pid: int = PID,
    *,
    reset: bool = False,
    drain: bool = True,
) -> tuple[usb.core.Device, int]:
    if reset:
        dev = bus_reset(vid, pid)
    else:
        dev = find_device(vid, pid)

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    ifn = intf.bInterfaceNumber

    if sys.platform.startswith("linux") and dev.is_kernel_driver_active(ifn):
        dev.detach_kernel_driver(ifn)

    usb.util.claim_interface(dev, ifn)

    if drain:
        drain_in(dev)

    return dev, ifn


def close_device(dev: usb.core.Device, ifn: int = 0) -> None:
    try:
        usb.util.release_interface(dev, ifn)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)


def run_text_command(
    dev: usb.core.Device,
    command: str,
    *,
    timeout_ms: int = 5000,
    drain_before: bool = True,
) -> str:
    if drain_before:
        drain_in(dev)

    payload = (command.strip() + "\n").encode("ascii")
    dev.write(EP_OUT, payload, timeout=timeout_ms)
    data = bytes(dev.read(EP_IN, 512, timeout=timeout_ms))
    return data.rstrip(b"\0").decode("ascii", errors="replace").rstrip()


def read_exact(dev: usb.core.Device, nbytes: int, timeout_ms: int) -> bytes:
    chunks: list[bytes] = []
    remaining = nbytes

    while remaining > 0:
        req = min(remaining, 16 * 1024)
        data = bytes(dev.read(EP_IN, req, timeout=timeout_ms))
        if not data:
            raise RuntimeError("short USB read")
        chunks.append(data)
        remaining -= len(data)

    return b"".join(chunks)
