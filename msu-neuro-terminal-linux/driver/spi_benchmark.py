#!/usr/bin/env python3
"""
Бенчмарк SPI транзакций Intan RHS2116 (фиксированно 25 МГц).
Использование: sudo python3 spi_benchmark.py
"""

import fcntl
import os
import struct
import sys

DEV = "/dev/intan"
BENCH_COUNT = 1000

# IOCTL
INTAN_IOC_MAGIC = ord("I")
_IOWR = lambda magic, nr, size: (3 << 30) | (size << 16) | (magic << 8) | nr

BENCH_STRUCT_SIZE = 24
BENCH_STRUCT_FMT = "I4xQI4x"
INTAN_IOC_BENCHMARK = _IOWR(INTAN_IOC_MAGIC, 3, BENCH_STRUCT_SIZE)


def main():
    if os.geteuid() != 0:
        print("Запуск с sudo для доступа к /dev/intan", file=sys.stderr)
        sys.exit(1)

    try:
        fd = os.open(DEV, os.O_RDWR)
    except OSError as e:
        print(f"Не удалось открыть {DEV}: {e}", file=sys.stderr)
        sys.exit(1)

    bench_buf = bytearray(struct.pack(BENCH_STRUCT_FMT, BENCH_COUNT, 0, 0))
    fcntl.ioctl(fd, INTAN_IOC_BENCHMARK, bench_buf)
    count, elapsed_ns, freq_hz = struct.unpack(BENCH_STRUCT_FMT, bytes(bench_buf))
    os.close(fd)

    elapsed_s = elapsed_ns / 1e9
    tx_per_sec = count / elapsed_s if elapsed_s > 0 else 0
    mbit_s = (count * 12 * 8 / 1e6) / elapsed_s if elapsed_s > 0 else 0
    us_per_tx = (elapsed_ns / 1000) / count if count else 0

    print("SPI бенчмарк Intan RHS2116 (25 МГц)")
    print("-" * 50)
    print(f"Транзакций/с: {tx_per_sec:,.0f}")
    print(f"Мбит/с:       {mbit_s:.1f}")
    print(f"мкс/транз:    {us_per_tx:.2f}")
    print("-" * 50)
    print("1 транзакция = 1 READ = 3 SPI transfer по 4 байта (12 байт)")


if __name__ == "__main__":
    main()
