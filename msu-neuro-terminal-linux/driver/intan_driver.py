#!/usr/bin/env python3
"""
Интерфейс к драйверу Intan (/dev/intan).
Ускоряет работу скриптов: 1 syscall вместо 3 на каждую операцию READ/WRITE/CONVERT.
Используется автоматически, если /dev/intan существует.
"""

import fcntl
import os
import struct
import ctypes
import errno

DEV = "/dev/intan"

# IOCTL
_IO = lambda m, n: (m << 8) | n
_IOW = lambda m, n, s: (1 << 30) | (s << 16) | (m << 8) | n
_IOWR = lambda m, n, s: (3 << 30) | (s << 16) | (m << 8) | n
_IOR = lambda m, n, s: (2 << 30) | (s << 16) | (m << 8) | n
MAGIC = ord("I")

# Структуры (совпадают с ядром)
READ_REG_FMT = "BBH"  # reg, pad, value — 4 байта
WRITE_REG_FMT = "BBBBH"  # reg, u_flag, m_flag, pad, value
CONVERT_FMT = "BBH"  # channel, flags, value

READ_REG_SZ = 4
WRITE_REG_SZ = 6  # reg(1)+u(1)+m(1)+pad(1)+value(2)
CONVERT_SZ = 4
SINGLE_STEP_FMT = "4B"
SINGLE_STEP_SZ = 4
PATTERN_OP_FMT = "=BBBBHH"
PATTERN_OP_SZ = struct.calcsize(PATTERN_OP_FMT)
RUN_PATTERN_ARG_FMT = "=IIQ"
RUN_PATTERN_ARG_SZ = struct.calcsize(RUN_PATTERN_ARG_FMT)
STREAM_CONFIG_FMT = "=IHH16sII"
STREAM_CONFIG_SZ = struct.calcsize(STREAM_CONFIG_FMT)
RING_LAYOUT_FMT = "=IIII"
RING_LAYOUT_SZ = struct.calcsize(RING_LAYOUT_FMT)
STREAM_STATUS_FMT = "=IIIIQQQQQ"
STREAM_STATUS_SZ = struct.calcsize(STREAM_STATUS_FMT)
STREAM_READ_PACKET_FMT = "=IIIIQ"
STREAM_READ_PACKET_SZ = struct.calcsize(STREAM_READ_PACKET_FMT)

INTAN_IOC_READ_REG = _IOWR(MAGIC, 4, READ_REG_SZ)
INTAN_IOC_WRITE_REG = _IOW(MAGIC, 5, WRITE_REG_SZ)
INTAN_IOC_CONVERT = _IOWR(MAGIC, 6, CONVERT_SZ)
INTAN_IOC_SINGLE_STEP = _IOW(MAGIC, 8, SINGLE_STEP_SZ)
INTAN_IOC_RUN_PATTERN = _IOWR(MAGIC, 9, RUN_PATTERN_ARG_SZ)
INTAN_IOC_STREAM_CONFIG = _IOW(MAGIC, 10, STREAM_CONFIG_SZ)
INTAN_IOC_GET_RING_LAYOUT = _IOR(MAGIC, 11, RING_LAYOUT_SZ)
INTAN_IOC_START_STREAM = _IO(MAGIC, 12)
INTAN_IOC_STOP_STREAM = _IO(MAGIC, 13)
INTAN_IOC_GET_STREAM_STATUS = _IOR(MAGIC, 14, STREAM_STATUS_SZ)
INTAN_IOC_STREAM_READ_PACKET = _IOWR(MAGIC, 15, STREAM_READ_PACKET_SZ)

INTAN_PATTERN_OP_WRITE_REG = 1
INTAN_PATTERN_OP_READ_REG = 2
INTAN_PATTERN_OP_CLEAR_ADC = 3
INTAN_PATTERN_OP_DELAY = 4
INTAN_PATTERN_OP_CLEAR_COMPLIANCE = 5

IMPEDANCE_MAX_AVERAGES = 1000
IMPEDANCE_MAX_SAMPLES = 2048
# Match the kernel struct layout exactly, including implicit padding/alignment:
# u8, u8, u16, u16, u16, u16, pad(2), u32, u16, pad(6)
IMPEDANCE_HEADER_FMT = "=BBHHHH2xIH6x"
IMPEDANCE_POINT_FMT = "qq"
IMPEDANCE_STRUCT_FMT = IMPEDANCE_HEADER_FMT + (IMPEDANCE_POINT_FMT * IMPEDANCE_MAX_AVERAGES)
IMPEDANCE_STRUCT_SZ = struct.calcsize(IMPEDANCE_STRUCT_FMT)
INTAN_IOC_MEASURE_IMPEDANCE = _IOWR(MAGIC, 7, IMPEDANCE_STRUCT_SZ)


class IntanDriver:
    """Драйвер Intan: 1 syscall на операцию вместо 3 через spidev."""

    def __init__(self, device=DEV):
        self.device = device
        self._fd = None
        self._ring_layout = None

    def open(self):
        self._fd = os.open(self.device, os.O_RDWR)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def read_reg(self, reg_addr):
        buf = bytearray(struct.pack(READ_REG_FMT, reg_addr & 0xFF, 0, 0))
        fcntl.ioctl(self._fd, INTAN_IOC_READ_REG, buf)
        _, _, value = struct.unpack(READ_REG_FMT, bytes(buf))
        return value

    def write_reg(self, reg_addr, value, u_flag=1, m_flag=0):
        buf = struct.pack(WRITE_REG_FMT, reg_addr & 0xFF, u_flag & 1, m_flag & 1, 0, value & 0xFFFF)
        fcntl.ioctl(self._fd, INTAN_IOC_WRITE_REG, buf)

    def transfer(self, data):
        """Для совместимости: raw 4-байтная команда, 3 фазы. Ответ 3-й фазы не возвращается."""
        os.write(self._fd, bytes(data[:4]) if len(data) >= 4 else bytes(4))

    def single_step(self, data):
        """Один SPI transfer без двух дополнительных dummy-фаз."""
        payload = bytes(data[:4]) if len(data) >= 4 else bytes(4)
        fcntl.ioctl(self._fd, INTAN_IOC_SINGLE_STEP, payload)

    def delay_step(self):
        """Один SPI-шаг задержки: single-transfer READ 255 без полного pipeline-read."""
        self.single_step([0xC0, 0xFF, 0x00, 0x00])

    def configure_stream(self, channels, sample_rate_hz, flags=0, ring_slot_count=0):
        channel_list = [int(ch) & 0xFF for ch in channels]
        if not channel_list or len(channel_list) > 16:
            raise ValueError("channels должен содержать 1..16 элементов")
        channel_bytes = bytes(channel_list + [0] * (16 - len(channel_list)))
        buf = struct.pack(
            STREAM_CONFIG_FMT,
            int(sample_rate_hz),
            len(channel_list),
            int(flags),
            channel_bytes,
            int(ring_slot_count),
            0,
        )
        fcntl.ioctl(self._fd, INTAN_IOC_STREAM_CONFIG, buf)
        self._ring_layout = None

    def get_ring_layout(self):
        if self._ring_layout is not None:
            return dict(self._ring_layout)
        buf = bytearray(RING_LAYOUT_SZ)
        fcntl.ioctl(self._fd, INTAN_IOC_GET_RING_LAYOUT, buf)
        slot_count, slot_bytes, packet_max_bytes, flags = struct.unpack(RING_LAYOUT_FMT, bytes(buf))
        self._ring_layout = {
            "slot_count": int(slot_count),
            "slot_bytes": int(slot_bytes),
            "packet_max_bytes": int(packet_max_bytes),
            "flags": int(flags),
        }
        return dict(self._ring_layout)

    def start_stream(self):
        fcntl.ioctl(self._fd, INTAN_IOC_START_STREAM, 0)

    def stop_stream(self):
        fcntl.ioctl(self._fd, INTAN_IOC_STOP_STREAM, 0)

    def get_stream_status(self):
        buf = bytearray(STREAM_STATUS_SZ)
        fcntl.ioctl(self._fd, INTAN_IOC_GET_STREAM_STATUS, buf)
        running, configured, last_errno, _, sequence, samples_produced, packets_produced, ring_overruns, spi_errors = struct.unpack(
            STREAM_STATUS_FMT,
            bytes(buf),
        )
        return {
            "running": bool(running),
            "configured": bool(configured),
            "last_errno": int(last_errno),
            "sequence": int(sequence),
            "samples_produced": int(samples_produced),
            "packets_produced": int(packets_produced),
            "ring_overruns": int(ring_overruns),
            "spi_errors": int(spi_errors),
        }

    def read_stream_packet(self, timeout_ms=100):
        layout = self.get_ring_layout()
        packet_buf = ctypes.create_string_buffer(layout["packet_max_bytes"])
        arg_buf = bytearray(struct.pack(
            STREAM_READ_PACKET_FMT,
            int(timeout_ms),
            layout["packet_max_bytes"],
            0,
            0,
            ctypes.addressof(packet_buf),
        ))
        try:
            fcntl.ioctl(self._fd, INTAN_IOC_STREAM_READ_PACKET, arg_buf)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EPIPE):
                return None
            raise
        _, _, packet_size, sequence, _ = struct.unpack(STREAM_READ_PACKET_FMT, bytes(arg_buf))
        return {
            "sequence": int(sequence),
            "data": packet_buf.raw[:packet_size],
        }

    def _normalize_pattern_op(self, op):
        return {
            "opcode": int(op.get("opcode", 0)),
            "reg": int(op.get("reg", 0)) & 0xFF,
            "flags": int(op.get("flags", 0)) & 0xFF,
            "reserved": int(op.get("reserved", 0)) & 0xFF,
            "value": int(op.get("value", 0)) & 0xFFFF,
            "count": int(op.get("count", 0)) & 0xFFFF,
        }

    def run_pattern(self, ops):
        """
        Выполняет список уже скомпилированных batch-операций через один ioctl.

        Каждый элемент `ops` — dict с ключами:
        - opcode
        - reg
        - flags
        - value
        - count
        """
        normalized_ops = [self._normalize_pattern_op(op) for op in ops]
        num_ops = len(normalized_ops)
        if num_ops == 0:
            return {"completed_ops": 0, "ops": []}

        ops_buf = ctypes.create_string_buffer(num_ops * PATTERN_OP_SZ)
        for idx, op in enumerate(normalized_ops):
            struct.pack_into(
                PATTERN_OP_FMT,
                ops_buf,
                idx * PATTERN_OP_SZ,
                op["opcode"],
                op["reg"],
                op["flags"],
                op["reserved"],
                op["value"],
                op["count"],
            )

        arg_buf = bytearray(struct.pack(
            RUN_PATTERN_ARG_FMT,
            num_ops,
            0,
            ctypes.addressof(ops_buf),
        ))
        fcntl.ioctl(self._fd, INTAN_IOC_RUN_PATTERN, arg_buf)
        _, completed_ops, _ = struct.unpack(RUN_PATTERN_ARG_FMT, bytes(arg_buf))

        executed_ops = []
        raw_ops = ops_buf.raw[:num_ops * PATTERN_OP_SZ]
        for idx in range(num_ops):
            offset = idx * PATTERN_OP_SZ
            opcode, reg, flags, reserved, value, count = struct.unpack_from(
                PATTERN_OP_FMT,
                raw_ops,
                offset,
            )
            executed_ops.append({
                "opcode": opcode,
                "reg": reg,
                "flags": flags,
                "reserved": reserved,
                "value": value,
                "count": count,
            })

        return {
            "completed_ops": int(completed_ops),
            "ops": executed_ops,
        }

    def read_reg_255(self):
        """Старый путь DELAY: полный READ регистра 255 через стандартный ioctl READ_REG."""
        return self.read_reg(0xFF)

    def clear_adc(self):
        os.write(self._fd, bytes([0x6A, 0x00, 0x00, 0x00]))

    def clear_compliance_monitor(self):
        os.write(self._fd, bytes([0xD0, 255, 0x00, 0x00]))

    def convert_channel(self, channel, amp_type="ac", h_flag=0):
        d_flag = 1 if amp_type == "dc" else 0
        flags = (d_flag << 1) | (h_flag & 1)
        buf = bytearray(struct.pack(CONVERT_FMT, channel & 0x3F, flags, 0))
        fcntl.ioctl(self._fd, INTAN_IOC_CONVERT, buf)
        _, _, value = struct.unpack(CONVERT_FMT, bytes(buf))
        return value

    def convert_channel_auto(self):
        buf = bytearray(struct.pack(CONVERT_FMT, 63, 0, 0))
        fcntl.ioctl(self._fd, INTAN_IOC_CONVERT, buf)
        _, _, value = struct.unpack(CONVERT_FMT, bytes(buf))
        return value

    def measure_impedance_raw(self, channel, scale_bits, num_samples=64, frequency_hz=1000, num_averages=1):
        """
        Пакетный замер импеданса в ядре.

        Возвращает dict:
        - points: list[{"sin_accum": int, "cos_accum": int}]
        - actual_num_averages: фактическое число усреднений в драйвере
        - actual_num_samples: фактическое число samples на одно усреднение
        - samples_per_period: число samples на период тестового синуса
        - effective_frequency_hz: фактическая частота тестового сигнала по всему batch

        scale_bits: 0=0.1pF, 1=1pF, 3=10pF
        """
        buf = bytearray(struct.pack(
            IMPEDANCE_STRUCT_FMT,
            channel & 0x0F,
            scale_bits,
            num_averages,
            num_samples,
            frequency_hz,
            0,
            0,
            0,
            *([0] * (2 * IMPEDANCE_MAX_AVERAGES))
        ))
        fcntl.ioctl(self._fd, INTAN_IOC_MEASURE_IMPEDANCE, buf)
        values = struct.unpack(IMPEDANCE_STRUCT_FMT, bytes(buf))
        actual_num_averages = int(values[2])
        actual_num_samples = int(values[3])
        samples_per_period = int(values[5])
        effective_frequency_hz = float(values[6]) / 1000.0
        point_values = values[8:8 + (actual_num_averages * 2)]
        points = [
            {"sin_accum": int(point_values[i]), "cos_accum": int(point_values[i + 1])}
            for i in range(0, len(point_values), 2)
        ]
        return {
            "points": points,
            "actual_num_averages": actual_num_averages,
            "actual_num_samples": actual_num_samples,
            "samples_per_period": samples_per_period,
            "effective_frequency_hz": effective_frequency_hz,
        }

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


def use_driver():
    """True, если /dev/intan доступен и нужно использовать драйвер."""
    return os.path.exists(DEV)
