#!/usr/bin/env python3
"""USB transport to STM32H743 Intan coprocessor (vendor bulk 0483:5741)."""

from __future__ import annotations

import re
import struct
import threading
from typing import Iterator, Optional

import usb.core

from intan_rhs1 import (
    Rhs1Frame,
    pack_rr8_multichannel,
    parse_rhs1_frame,
    rhs1_frame_to_payload_bytes,
)
from usb_intan_lib import (
    EP_OUT,
    FRAME_SIZE,
    PID,
    VID,
    close_device,
    drain_in,
    open_device,
    read_exact,
    run_text_command,
)

INTAN_CHIP_ID = 0x0020
INTAN_CHIP_ID_REG = 255

# Один 32-битный RHS2116 SPI slot при SCK 25 MHz.
INTAN_SPI_SCK_HZ = 25_000_000
INTAN_SPI_SLOT_US = max(
    1, (32 * 1_000_000 + INTAN_SPI_SCK_HZ - 1) // INTAN_SPI_SCK_HZ
)


def normalize_stim_current_value(reg: int, value: int) -> int:
    """Привести magnitude регистр 64-111 к формату 0x80XX (как в прошивке)."""
    if (64 <= reg <= 79 or 96 <= reg <= 111) and (value < 0x8000 or value > 0x80FF):
        if value > 255:
            raise ValueError(
                f"Значение тока для регистра {reg} должно быть 0-255 µA "
                f"или 0x80XX, получено: {value}"
            )
        value = 0x8000 | (value & 0xFF)
    return value & 0xFFFF


def encode_intan_write_raw_word(
    reg: int, value: int, u_flag: int = 0, m_flag: int = 0
) -> int:
    """
    Одно 32-битное WRITE-слово для PATTERN_ADD_RAW (STM32_SPI_CO guide).

    word = (header << 24) | (reg << 16) | value
    header = 0x80 | (U << 5) | (M << 4)
    """
    value = normalize_stim_current_value(reg, value)
    header = 0x80 | ((int(u_flag) & 1) << 5) | ((int(m_flag) & 1) << 4)
    return ((header & 0xFF) << 24) | ((reg & 0xFF) << 16) | value


def intan_spi_slots_to_us(slots: int) -> int:
    """DELAY N (в слотах SPI) -> микросекунды для PATTERN_ADD_DELAY_US."""
    if slots <= 0:
        return 0
    return max(1, (slots * 32 * 1_000_000 + INTAN_SPI_SCK_HZ - 1) // INTAN_SPI_SCK_HZ)


# Перезапуск SPI_STREAM_* при длинной записи
V2_STREAM_RELOAD_SAMPLES = 10_000_000


class IntanUsbError(RuntimeError):
    pass


class IntanUsbTransport:
    """
    USB V2 (RHS1): PING, SPI_STREAM_RR8_REAL, STOP, STATS + Intan ID/READ/WRITE/INIT_*.
    USB V1 (legacy): STREAM / STREAM8 + те же Intan-команды.
    """

    def __init__(
        self,
        vid: int = VID,
        pid: int = PID,
        *,
        reset_on_open: bool = True,
        timeout_ms: int = 5000,
        stream_timeout_ms: int = 60000,
        verbose: bool = False,
    ):
        self.vid = vid
        self.pid = pid
        self.reset_on_open = reset_on_open
        self.timeout_ms = timeout_ms
        self.stream_timeout_ms = stream_timeout_ms
        self.verbose = verbose
        self._dev = None
        self._ifn = 0
        self._lock = threading.RLock()
        self._open_count = 0
        self._firmware: str | None = None  # "v2" | "v1"
        self._v2_stream_active = False
        self._v2_stream_remaining = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[USB] {msg}")

    def open(self) -> None:
        with self._lock:
            if self._open_count == 0:
                self._dev, self._ifn = open_device(
                    self.vid, self.pid, reset=self.reset_on_open
                )
                self._firmware = None
                self._log(f"opened {self.vid:04x}:{self.pid:04x}")
            self._open_count += 1

    def close(self) -> None:
        with self._lock:
            if self._open_count <= 0:
                return
            self._open_count -= 1
            if self._open_count == 0:
                try:
                    if self._firmware == "v2":
                        self.stop_stream()
                except Exception:
                    pass
                if self._dev is not None:
                    close_device(self._dev, self._ifn)
                    self._dev = None
                self._log("closed")

    def __enter__(self) -> IntanUsbTransport:
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._dev is None:
            raise IntanUsbError("USB transport not open")

    def firmware_version(self) -> str:
        with self._lock:
            if self._firmware is not None:
                return self._firmware
            self._ensure_open()
            reply = run_text_command(
                self._dev, "PING", timeout_ms=self.timeout_ms, drain_before=True
            )
            stripped = reply.strip()
            if stripped == "PONG":
                self._firmware = "v2"
            elif stripped.startswith("OK") and "PONG" in stripped:
                self._firmware = "v1"
            elif "PONG" in stripped:
                self._firmware = "v1"
            else:
                raise IntanUsbError(f"unexpected PING reply: {reply!r}")
            self._log(f"firmware={self._firmware}")
            return self._firmware

    @staticmethod
    def _parse_ok_v1(reply: str) -> None:
        if reply.startswith("ERR "):
            raise IntanUsbError(reply)
        if not reply.startswith("OK"):
            raise IntanUsbError(f"unexpected reply: {reply!r}")

    @staticmethod
    def _parse_intan_reply(reply: str) -> None:
        if reply.startswith("ERR "):
            raise IntanUsbError(reply)

    def run_command(self, command: str, *, timeout_ms: Optional[int] = None) -> str:
        with self._lock:
            self._ensure_open()
            tout = self.timeout_ms if timeout_ms is None else timeout_ms
            reply = run_text_command(self._dev, command, timeout_ms=tout)
            if self.firmware_version() == "v1":
                self._parse_ok_v1(reply)
            return reply

    def run_intan_command(self, command: str, *, timeout_ms: Optional[int] = None) -> str:
        """Intan-команда с проверкой ERR (V1 и V2)."""
        reply = self.run_command(command, timeout_ms=timeout_ms)
        self._parse_intan_reply(reply)
        if self.firmware_version() == "v1":
            self._parse_ok_v1(reply)
        elif not reply.startswith("OK"):
            raise IntanUsbError(f"unexpected Intan reply: {reply!r}")
        return reply

    def ping(self) -> None:
        reply = self.run_command("PING")
        if "PONG" not in reply:
            raise IntanUsbError(reply)

    def stats(self) -> str:
        return self.run_command("STATS", timeout_ms=self.timeout_ms)

    def stop_stream(self) -> None:
        with self._lock:
            self._ensure_open()
            if self.firmware_version() != "v2":
                return
            self._v2_stream_active = False
            self._v2_stream_remaining = 0
            try:
                # STOP сразу: drain_before=True читает весь активный поток (секунды).
                run_text_command(
                    self._dev, "STOP", timeout_ms=self.timeout_ms, drain_before=False
                )
            except Exception:
                pass
            drain_in(self._dev, timeout_ms=10)

    def _resync_pipeline_unlocked(self) -> None:
        """
        Выровнять RHS2116 SPI pipeline на host после PATTERN_ADD_RAW / PATTERN_RUN.

        RAW-паттерны шлют по одному 32-bit слоту на WRITE и сбивают 2-slot pipeline;
        без resync READ 255 может вернуть 0x0030 вместо chip ID 0x0020.
        """
        try:
            run_text_command(self._dev, "INIT_STIM", timeout_ms=15000)
            self._log("pipeline resync via INIT_STIM")
            return
        except Exception:
            pass
        try:
            run_text_command(self._dev, "CLEAR_ADC", timeout_ms=self.timeout_ms)
        except Exception:
            pass
        for _ in range(6):
            try:
                run_text_command(self._dev, "READ 255", timeout_ms=self.timeout_ms)
            except Exception:
                pass

    def resync_pipeline(self) -> None:
        with self._lock:
            self._ensure_open()
            self._resync_pipeline_unlocked()
            self._log("pipeline resync")

    @staticmethod
    def _parse_read_value(reply: str) -> int:
        match = re.search(r"value=0x([0-9A-Fa-f]+)", reply)
        if not match:
            raise IntanUsbError(f"cannot parse READ: {reply!r}")
        return int(match.group(1), 16)

    @staticmethod
    def _parse_chip_id_value(reply: str) -> int:
        match = re.search(r"chip=0x([0-9A-Fa-f]+)", reply)
        if not match:
            raise IntanUsbError(f"cannot parse ID: {reply!r}")
        return int(match.group(1), 16)

    def chip_id(self) -> int:
        with self._lock:
            self._ensure_open()
            for attempt in range(5):
                if attempt > 0:
                    self._resync_pipeline_unlocked()
                reply = run_text_command(self._dev, "ID", timeout_ms=self.timeout_ms)
                self._parse_intan_reply(reply)
                if not reply.startswith("OK"):
                    raise IntanUsbError(f"unexpected ID reply: {reply!r}")
                cid = self._parse_chip_id_value(reply)
                if cid == INTAN_CHIP_ID:
                    return cid
                self._log(
                    f"chip ID 0x{cid:04X} != 0x{INTAN_CHIP_ID:04X}, "
                    f"resync attempt {attempt + 1}"
                )
            raise IntanUsbError(
                f"chip ID 0x{cid:04X} (expected 0x{INTAN_CHIP_ID:04X}) "
                f"after pipeline resync"
            )

    def read_register(self, reg: int, *, expect_chip_id: bool = False) -> int:
        with self._lock:
            self._ensure_open()
            attempts = 5 if expect_chip_id and reg == INTAN_CHIP_ID_REG else 1
            last_val = 0
            for attempt in range(attempts):
                if attempt > 0:
                    self._resync_pipeline_unlocked()
                reply = run_text_command(
                    self._dev, f"READ {reg}", timeout_ms=self.timeout_ms
                )
                self._parse_intan_reply(reply)
                if not reply.startswith("OK"):
                    raise IntanUsbError(f"unexpected READ reply: {reply!r}")
                last_val = self._parse_read_value(reply)
                if not expect_chip_id or reg != INTAN_CHIP_ID_REG or last_val == INTAN_CHIP_ID:
                    return last_val
                self._log(
                    f"READ 255 = 0x{last_val:04X}, expected 0x{INTAN_CHIP_ID:04X}, "
                    f"resync attempt {attempt + 1}"
                )
            raise IntanUsbError(
                f"READ 255 = 0x{last_val:04X} (expected 0x{INTAN_CHIP_ID:04X}) "
                f"after pipeline resync"
            )

    def write_register(
        self, reg: int, value: int, u_flag: int = 0, m_flag: int = 0
    ) -> None:
        self.run_intan_command(f"WRITE {reg} {value:#x} {u_flag} {m_flag}")

    def pattern_clear(self) -> None:
        self.run_intan_command("PATTERN_CLEAR")

    def pattern_add_raw_word(self, word: int) -> None:
        self.run_intan_command(f"PATTERN_ADD_RAW {int(word) & 0xFFFFFFFF:#x}")

    def pattern_add_write(
        self, reg: int, value: int, u_flag: int = 0, m_flag: int = 0
    ) -> None:
        self.run_intan_command(
            f"PATTERN_ADD_WRITE {reg} {value:#x} {int(u_flag)} {int(m_flag)}"
        )

    def pattern_add_read(self, reg: int) -> None:
        self.run_intan_command(f"PATTERN_ADD_READ {reg}")

    def pattern_add_clear_adc(self) -> None:
        self.run_intan_command("PATTERN_ADD_CLEAR_ADC")

    def pattern_add_clear_comp(self) -> None:
        self.run_intan_command("PATTERN_ADD_CLEAR_COMP")

    def pattern_add_delay_us(self, delay_us: int) -> None:
        self.run_intan_command(f"PATTERN_ADD_DELAY_US {int(delay_us)}")

    def pattern_add_delay_cycles(self, cycles: int) -> None:
        self.run_intan_command(f"PATTERN_ADD_DELAY_CYC {int(cycles)}")

    def pattern_run(
        self, repeat_count: int = 1, *, timeout_ms: Optional[int] = None
    ) -> str:
        tout = timeout_ms
        if tout is None:
            tout = max(60_000, int(repeat_count) * 500)
        return self.run_intan_command(
            f"PATTERN_RUN {int(repeat_count)}",
            timeout_ms=tout,
        )

    def pattern_status(self) -> str:
        return self.run_intan_command("PATTERN_STATUS")

    def convert(self, channel: int, flags: int = 0) -> int:
        reply = self.run_intan_command(f"CONVERT {channel} {flags}")
        value_match = re.search(r"value=0x([0-9A-Fa-f]+)", reply)
        if not value_match:
            raise IntanUsbError(f"cannot parse CONVERT: {reply}")
        ch_match = re.search(r"\bch=(\d+)", reply)
        if ch_match and int(ch_match.group(1)) != int(channel):
            raise IntanUsbError(
                f"CONVERT channel mismatch: requested {channel}, reply {reply!r}"
            )
        return int(value_match.group(1), 16)

    def convert_channel(self, channel: int, amp_type: str = "ac", h_flag: int = 0) -> int:
        d_flag = 1 if amp_type == "dc" else 0
        flags = (h_flag & 1) | ((d_flag & 1) << 1)
        return self.convert(channel, flags)

    def convert_channel_auto(self) -> int:
        return self.convert(63, 0)

    def uses_firmware_impedance(self) -> bool:
        try:
            return self.firmware_version() == "v2"
        except Exception:
            return False

    def _measure_impedance_firmware(
        self,
        channel: int,
        scale_bits: int,
        *,
        num_samples: int,
        frequency_hz: int,
        num_averages: int,
        phase_safe: bool,
        restore_regs: bool,
    ) -> dict:
        from intan_impedance import (
            parse_impedance_measure_reply,
            select_impedance_profile,
        )

        samples_per_period, periods = select_impedance_profile(
            int(frequency_hz), int(num_samples)
        )
        flags = (1 if phase_safe else 0) | (2 if restore_regs else 0)
        command = (
            f"IMPEDANCE_MEASURE {channel} {scale_bits} {frequency_hz} "
            f"{samples_per_period} {periods} {flags}"
        )

        points: list[dict] = []
        total_overruns = 0
        total_spi_errors = 0
        total_clipped = 0
        last_meta: dict = {}

        for _ in range(int(num_averages)):
            reply = self.run_intan_command(command, timeout_ms=self.stream_timeout_ms)
            raw = parse_impedance_measure_reply(reply)
            if raw.get("overruns", 0) or raw.get("spi_errors", 0):
                raise IntanUsbError(
                    f"IMPEDANCE_MEASURE invalid: overruns={raw.get('overruns')} "
                    f"spi_errors={raw.get('spi_errors')} clipped={raw.get('clipped')}"
                )
            point = raw.get("points", [{}])[0]
            points.append(point)
            total_overruns += int(raw.get("overruns", 0))
            total_spi_errors += int(raw.get("spi_errors", 0))
            total_clipped += int(raw.get("clipped", 0))
            last_meta = raw

        return {
            "points": points,
            "actual_num_averages": int(num_averages),
            "actual_num_samples": int(last_meta.get("actual_num_samples", 0)),
            "samples_per_period": int(last_meta.get("samples_per_period", samples_per_period)),
            "effective_frequency_hz": float(
                last_meta.get("effective_frequency_hz", frequency_hz)
            ),
            "overruns": total_overruns,
            "spi_errors": total_spi_errors,
            "clipped": total_clipped,
            "firmware": True,
        }

    def measure_impedance_raw(
        self,
        channel: int,
        scale_bits: int,
        num_samples: int = 64,
        frequency_hz: int = 1000,
        num_averages: int = 1,
        *,
        phase_safe: bool = True,
        restore_regs: bool = True,
    ) -> dict:
        with self._lock:
            if self.uses_firmware_impedance():
                self.stop_stream()
                return self._measure_impedance_firmware(
                    channel,
                    scale_bits,
                    num_samples=num_samples,
                    frequency_hz=int(frequency_hz),
                    num_averages=int(num_averages),
                    phase_safe=phase_safe,
                    restore_regs=restore_regs,
                )

            from intan_impedance import measure_impedance_raw as _measure_raw
            from stimulate_channel0 import clear_adc, write_intan_register

            return _measure_raw(
                self,
                channel,
                scale_bits,
                num_samples=num_samples,
                frequency_hz=int(frequency_hz),
                num_averages=int(num_averages),
                write_register=write_intan_register,
                convert=lambda ch: self.convert(ch, 0),
                clear_adc=clear_adc,
                stop_stream=None,
            )

    def init_record(self, ksps: int = 350) -> None:
        if self.firmware_version() == "v2":
            self.stop_stream()
        self.run_intan_command(f"INIT_RECORD {ksps}", timeout_ms=15000)

    def init_stim(self) -> None:
        if self.firmware_version() == "v2":
            self.stop_stream()
        self.run_intan_command("INIT_STIM", timeout_ms=15000)

    def clear_adc(self) -> None:
        if self.firmware_version() == "v2":
            self.stop_stream()
        self.run_intan_command("CLEAR_ADC")

    def clear_comp(self) -> None:
        if self.firmware_version() == "v2":
            self.stop_stream()
        self.run_intan_command("CLEAR_COMP")

    def _start_v2_stream(self, command_line: str, n_samples: int) -> None:
        self._ensure_open()
        self.stop_stream()
        reply = run_text_command(
            self._dev,
            command_line,
            timeout_ms=self.timeout_ms,
            drain_before=True,
        )
        self._parse_intan_reply(reply)
        if not reply.startswith("OK"):
            raise IntanUsbError(f"unexpected stream reply: {reply!r}")
        self._v2_stream_active = True
        self._v2_stream_remaining = n_samples

    def start_spi_stream_rr8_real(self, n_samples: int, flags: int = 0) -> None:
        with self._lock:
            if self.firmware_version() != "v2":
                raise IntanUsbError("SPI_STREAM_RR8_REAL только в прошивке V2")
            self._start_v2_stream(
                f"SPI_STREAM_RR8_REAL {n_samples} {flags}", n_samples
            )

    def start_spi_stream_rr16_real(self, n_samples: int, flags: int = 0) -> None:
        with self._lock:
            if self.firmware_version() != "v2":
                raise IntanUsbError("SPI_STREAM_RR16_REAL только в прошивке V2")
            self._start_v2_stream(
                f"SPI_STREAM_RR16_REAL {n_samples} {flags}", n_samples
            )

    def start_spi_stream_range_real(
        self, n_samples: int, first_channel: int, channel_count: int, flags: int = 0
    ) -> None:
        with self._lock:
            if self.firmware_version() != "v2":
                raise IntanUsbError("SPI_STREAM_RANGE_REAL только в прошивке V2")
            self._start_v2_stream(
                f"SPI_STREAM_RANGE_REAL {n_samples} {first_channel} {channel_count} {flags}",
                n_samples,
            )

    def start_spi_stream_real(
        self, n_samples: int, channel: int, flags: int = 0
    ) -> None:
        with self._lock:
            if self.firmware_version() != "v2":
                raise IntanUsbError("SPI_STREAM_REAL только в прошивке V2")
            self._start_v2_stream(
                f"SPI_STREAM_REAL {n_samples} {channel} {flags}", n_samples
            )

    def _consume_v2_raw(self, payload: bytes) -> None:
        if self._v2_stream_remaining <= 0:
            return
        sample_count = struct.unpack_from("<I", payload, 16)[0]
        self._v2_stream_remaining = max(0, self._v2_stream_remaining - sample_count)
        if self._v2_stream_remaining == 0:
            self._v2_stream_active = False

    def read_rhs1_raw_burst(
        self,
        timeout_ms: Optional[int] = None,
        *,
        max_extra: int = 0,
    ) -> list[bytes]:
        """Читает один или несколько RHS1-кадров без parse (быстрый hot path)."""
        with self._lock:
            self._ensure_open()
            if self.firmware_version() != "v2":
                raise IntanUsbError("RHS1 frames только в прошивке V2")
            tout = self.stream_timeout_ms if timeout_ms is None else timeout_ms
            frames: list[bytes] = []
            try:
                payload = read_exact(self._dev, FRAME_SIZE, timeout_ms=tout)
            except usb.core.USBTimeoutError:
                return frames
            if len(payload) != FRAME_SIZE:
                raise IntanUsbError(f"short RHS1 frame: {len(payload)} bytes")
            frames.append(payload)
            self._consume_v2_raw(payload)

            for _ in range(max_extra):
                try:
                    extra = read_exact(self._dev, FRAME_SIZE, timeout_ms=1)
                except usb.core.USBTimeoutError:
                    break
                if len(extra) != FRAME_SIZE:
                    raise IntanUsbError(f"short RHS1 frame: {len(extra)} bytes")
                frames.append(extra)
                self._consume_v2_raw(extra)
            return frames

    def read_rhs1_raw(self, timeout_ms: Optional[int] = None) -> Optional[bytes]:
        burst = self.read_rhs1_raw_burst(timeout_ms=timeout_ms, max_extra=0)
        return burst[0] if burst else None

    def read_rhs1_frame(self, timeout_ms: Optional[int] = None) -> Optional[Rhs1Frame]:
        burst = self.read_rhs1_raw_burst(timeout_ms=timeout_ms, max_extra=0)
        if not burst:
            return None
        return parse_rhs1_frame(burst[0])

    def iter_rhs1_frames(
        self, timeout_ms: Optional[int] = None
    ) -> Iterator[Rhs1Frame]:
        while True:
            frame = self.read_rhs1_frame(timeout_ms=timeout_ms)
            if frame is None:
                break
            yield frame

    def stream(
        self,
        n_samples: int,
        channel: int = 0,
        flags: int = 0,
        *,
        ch_last: Optional[int] = None,
    ) -> bytes:
        if n_samples <= 0:
            raise ValueError("n_samples must be > 0")

        if self.firmware_version() == "v2":
            raise IntanUsbError(
                "legacy STREAM недоступен в V2; используйте iter_rhs1_frames()"
            )

        with self._lock:
            self._ensure_open()
            if ch_last is not None:
                cmd = f"STREAM {n_samples} {channel} {ch_last} {flags}\n".encode("ascii")
            else:
                cmd = f"STREAM {n_samples} {channel} {flags}\n".encode("ascii")
            self._dev.write(EP_OUT, cmd, timeout=self.timeout_ms)
            return read_exact(
                self._dev, n_samples * 2, timeout_ms=self.stream_timeout_ms
            )

    def stream8(self, n_samples: int, flags: int = 0) -> bytes:
        if n_samples <= 0:
            raise ValueError("n_samples must be > 0")

        if self.firmware_version() == "v2":
            chunks: list[bytes] = []
            collected = 0
            self.start_spi_stream_rr8_real(n_samples, flags)
            while collected < n_samples:
                frame = self.read_rhs1_frame()
                if frame is None:
                    if not self._v2_stream_active:
                        break
                    continue
                need = n_samples - collected
                take = min(need, frame.sample_count)
                if frame.channel_tagged:
                    adcs = [sample.adc for sample in frame.tagged_samples[:take]]
                    part = struct.pack(f"<{len(adcs)}H", *adcs) if adcs else b""
                else:
                    part = rhs1_frame_to_payload_bytes(frame, max_samples=take)
                if not part:
                    continue
                chunks.append(part)
                collected += take
            return b"".join(chunks)

        with self._lock:
            self._ensure_open()
            cmd = f"STREAM8 {n_samples} {flags}\n".encode("ascii")
            self._dev.write(EP_OUT, cmd, timeout=self.timeout_ms)
            return read_exact(
                self._dev, n_samples * 2, timeout_ms=self.stream_timeout_ms
            )

    def verify_chip(self) -> None:
        self.ping()
        self.resync_pipeline()
        cid = self.chip_id()
        if cid != INTAN_CHIP_ID:
            raise IntanUsbError(
                f"unexpected chip ID 0x{cid:04X} (expected 0x{INTAN_CHIP_ID:04X})"
            )

    def after_pattern_run(self) -> None:
        """Вызывать после PATTERN_RUN, если дальше нужны READ/ID/стим-команды."""
        self.resync_pipeline()
