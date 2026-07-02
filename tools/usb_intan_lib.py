"""PyUSB helpers for STM32H743 RHS1 vendor bulk (USB HS streaming V2)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import sys
import time
import struct

import usb.core
import usb.util

VID = 0x0483
PID = 0x5741
EP_OUT = 0x01
EP_IN = 0x81
FRAME_SIZE = 4096
FRAME_MAGIC = 0x52485331
FRAME_VERSION = 1
_RHS1_HEADER = struct.Struct("<IHHIIIIII")
_RHS1_FLAG_CHANNEL_TAG = 0x0008
_RHS1_RR8_FIRST_CHANNEL = 0
_RHS1_RR8_CHANNELS = 8


@dataclass
class Rhs1FwDecodeState:
    """State for decoding RR8 FW samples across RHS1 frame boundaries."""

    expected_seq: int | None = 0
    global_idx: int = 0
    seq_gaps: int = 0
    strict_seq: bool = False


def validate_rhs1_frame(frame: bytes, expected_frame_seq: int | None = None) -> tuple[int, ...]:
    """Validate one complete RHS1 frame and return its unpacked 32-byte header."""
    if len(frame) != FRAME_SIZE:
        raise RuntimeError(f"RHS1 short frame: got {len(frame)} bytes, expected {FRAME_SIZE}")

    header = _RHS1_HEADER.unpack_from(frame)
    magic, version, flags, frame_seq, _, sample_count, _, _, _ = header
    if magic != FRAME_MAGIC:
        raise RuntimeError(f"RHS1 bad magic: 0x{magic:08X}, expected 0x{FRAME_MAGIC:08X}")
    if version != FRAME_VERSION:
        raise RuntimeError(f"RHS1 unsupported version: {version}, expected {FRAME_VERSION}")
    max_samples = 1016 if (flags & _RHS1_FLAG_CHANNEL_TAG) else 2032
    if sample_count > max_samples:
        raise RuntimeError(f"RHS1 invalid sample_count: {sample_count}, max {max_samples}")
    if expected_frame_seq is not None and frame_seq != (expected_frame_seq & 0xFFFFFFFF):
        raise RuntimeError(f"RHS1 frame sequence gap: got {frame_seq}, expected {expected_frame_seq & 0xFFFFFFFF}")
    return header


def iter_rhs1_fw_samples(
    frame: bytes,
    state: Rhs1FwDecodeState,
    *,
    n_ch: int = _RHS1_RR8_CHANNELS,
) -> Iterator[tuple[int, int]]:
    """Decode one RHS1 RR8 frame into (channel, adc_code) samples.

    Untagged frames do not carry channel numbers, so channel assignment must use
    the frame header phase. Host-side dropped USB frames must not shift channels.
    """
    header = validate_rhs1_frame(frame, None)
    _, _, flags, frame_seq, first_sc, sample_count, _, _, meta = header

    if state.expected_seq is not None:
        expected = state.expected_seq & 0xFFFFFFFF
        if frame_seq != expected:
            state.seq_gaps += 1
            if state.strict_seq:
                raise RuntimeError(f"RHS1 frame sequence gap: got {frame_seq}, expected {expected}")
        state.expected_seq = frame_seq + 1

    tagged = (flags & _RHS1_FLAG_CHANNEL_TAG) != 0
    if tagged:
        first_ch = meta & 0xFF
        ch_count = (meta >> 8) & 0xFF
        if first_ch != _RHS1_RR8_FIRST_CHANNEL or ch_count != n_ch:
            raise RuntimeError(f"bad RR8 meta 0x{meta:08X}")

    for i in range(sample_count):
        if tagged:
            word = struct.unpack_from("<I", frame, 32 + i * 4)[0]
            ch = (word >> 16) & 0xF
            adc = word & 0xFFFF
        else:
            adc = struct.unpack_from("<H", frame, 32 + i * 2)[0]
            ch = (first_sc + i) % n_ch
        state.global_idx += 1
        yield ch, adc


def find_device(vid: int = VID, pid: int = PID) -> usb.core.Device:
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"device {vid:04x}:{pid:04x} not found")
    return dev


def bus_reset(vid: int = VID, pid: int = PID, settle_s: float = 1.5) -> usb.core.Device:
    dev = find_device(vid, pid)
    try:
        dev.reset()
    except usb.core.USBError:
        pass
    time.sleep(settle_s)
    return find_device(vid, pid)


def drain_in(dev: usb.core.Device, timeout_ms: int = 50) -> int:
    total = 0
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=timeout_ms))
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
    dev = bus_reset(vid, pid) if reset else find_device(vid, pid)
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


def read_text_during_stream(dev: usb.core.Device, command: str, timeout_ms: int = 5000) -> str:
    """Send a command while RHS1 frames may be on IN; return first text reply."""
    payload = (command.strip() + "\n").encode("ascii")
    dev.write(EP_OUT, payload, timeout=timeout_ms)
    deadline = time.perf_counter() + timeout_ms / 1000.0
    while time.perf_counter() < deadline:
        try:
            data = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=500))
        except Exception:
            break
        if len(data) < 64 and data[:1].isalpha():
            return data.rstrip(b"\0").decode("ascii", errors="replace").strip()
    return ""


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
        req = min(remaining, FRAME_SIZE)
        data = bytes(dev.read(EP_IN, req, timeout=timeout_ms))
        if not data:
            raise RuntimeError("short USB read")
        chunks.append(data)
        remaining -= len(data)

    return b"".join(chunks)
