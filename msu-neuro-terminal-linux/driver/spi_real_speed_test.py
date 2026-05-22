#!/usr/bin/env python3
"""
Тест реальной скорости SPI на живом устройстве.

Скрипт измеряет не только настроенную частоту, но и фактическую скорость
по тактам SCK, время на одну логическую операцию и утилизацию шины.

Поддерживаемые backend:
- /dev/intan: ioctl-бенчмарк драйвера, одна логическая операция = READ
  регистра (3 SPI transfer по 4 байта, то есть 12 байт = 96 тактов).
- /dev/spidevX.Y: цикл xfer2(), где логическая операция задается числом
  transfer и размером каждого transfer.

Примеры:
  python3 spi_real_speed_test.py
  python3 spi_real_speed_test.py --device /dev/intan --count 20000 --repeat 7
  python3 spi_real_speed_test.py --device /dev/spidev1.1 --speed-hz 10000000
  python3 spi_real_speed_test.py --device /dev/spidev1.1 --payload-bytes 4 --transfers-per-op 20
"""

import argparse
import fcntl
import os
import statistics
import struct
import sys
import time

try:
    import spidev
except ImportError:
    spidev = None


DEFAULT_INTAN_DEVICE = "/dev/intan"
DEFAULT_SPIDEV_DEVICE = "/dev/spidev1.1"
INTAN_CLOCKED_BYTES_PER_OP = 12

INTAN_IOC_MAGIC = ord("I")
_IOWR = lambda magic, nr, size: (3 << 30) | (size << 16) | (magic << 8) | nr
BENCH_STRUCT_SIZE = 24
BENCH_STRUCT_FMT = "I4xQI4x"
INTAN_IOC_BENCHMARK = _IOWR(INTAN_IOC_MAGIC, 3, BENCH_STRUCT_SIZE)


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Значение должно быть > 0")
    return parsed


def choose_device(requested_device):
    if requested_device != "auto":
        return requested_device
    if os.path.exists(DEFAULT_INTAN_DEVICE):
        return DEFAULT_INTAN_DEVICE
    return DEFAULT_SPIDEV_DEVICE


def infer_backend(device):
    if os.path.basename(device) == "intan":
        return "intan"
    if "spidev" in os.path.basename(device):
        return "spidev"
    raise ValueError(
        f"Не удалось определить backend по устройству {device}. "
        "Укажите /dev/intan или /dev/spidevX.Y."
    )


def parse_spidev_path(device):
    name = os.path.basename(device)
    if not name.startswith("spidev") or "." not in name:
        raise ValueError(f"Ожидался путь вида /dev/spidevX.Y, получено: {device}")
    bus_str, dev_str = name[len("spidev"):].split(".", 1)
    return int(bus_str), int(dev_str)


def run_intan_once(device, count):
    fd = os.open(device, os.O_RDWR)
    try:
        bench_buf = bytearray(struct.pack(BENCH_STRUCT_FMT, count, 0, 0))
        fcntl.ioctl(fd, INTAN_IOC_BENCHMARK, bench_buf)
        actual_count, elapsed_ns, freq_hz = struct.unpack(BENCH_STRUCT_FMT, bytes(bench_buf))
    finally:
        os.close(fd)

    return {
        "backend": "intan",
        "device": device,
        "count": actual_count,
        "elapsed_s": elapsed_ns / 1e9,
        "configured_clock_hz": float(freq_hz),
        "clocked_bytes_per_op": INTAN_CLOCKED_BYTES_PER_OP,
        "aggregate_payload_bytes_per_op": None,
        "logical_op_label": "READ register (3 x 4 B SPI)",
    }


def run_spidev_once(device, count, speed_hz, payload_bytes, transfers_per_op, warmup_ops):
    if spidev is None:
        raise RuntimeError(
            "Модуль spidev не установлен. Установите python3-spidev "
            "или используйте backend /dev/intan."
        )

    bus, dev = parse_spidev_path(device)
    spi = spidev.SpiDev()
    spi.open(bus, dev)
    spi.max_speed_hz = speed_hz
    spi.mode = 0
    spi.bits_per_word = 8

    payload = [0x00] * payload_bytes
    try:
        for _ in range(warmup_ops):
            for _ in range(transfers_per_op):
                spi.xfer2(payload)

        t0 = time.perf_counter()
        for _ in range(count):
            for _ in range(transfers_per_op):
                spi.xfer2(payload)
        t1 = time.perf_counter()
    finally:
        spi.close()

    clocked_bytes_per_op = payload_bytes * transfers_per_op
    return {
        "backend": "spidev",
        "device": device,
        "count": count,
        "elapsed_s": t1 - t0,
        "configured_clock_hz": float(speed_hz),
        "clocked_bytes_per_op": clocked_bytes_per_op,
        "aggregate_payload_bytes_per_op": clocked_bytes_per_op * 2,
        "logical_op_label": f"{transfers_per_op} x xfer2({payload_bytes} B)",
    }


def enrich_measurement(raw):
    count = max(1, int(raw["count"]))
    elapsed_s = max(float(raw["elapsed_s"]), 1e-12)
    configured_clock_hz = max(float(raw["configured_clock_hz"]), 1.0)
    clocked_bits_per_op = int(raw["clocked_bytes_per_op"]) * 8
    total_clocked_bits = count * clocked_bits_per_op
    effective_sck_hz = total_clocked_bits / elapsed_s
    us_per_op = (elapsed_s / count) * 1e6
    ops_per_sec = count / elapsed_s
    theoretical_min_us_per_op = (clocked_bits_per_op / configured_clock_hz) * 1e6
    software_overhead_us = us_per_op - theoretical_min_us_per_op
    utilization_pct = (effective_sck_hz / configured_clock_hz) * 100.0

    aggregate_mbps = None
    aggregate_payload_bytes_per_op = raw.get("aggregate_payload_bytes_per_op")
    if aggregate_payload_bytes_per_op:
        aggregate_mbps = (count * aggregate_payload_bytes_per_op * 8) / elapsed_s / 1e6

    enriched = dict(raw)
    enriched.update({
        "clocked_bits_per_op": clocked_bits_per_op,
        "effective_sck_hz": effective_sck_hz,
        "effective_sck_mhz": effective_sck_hz / 1e6,
        "us_per_op": us_per_op,
        "ops_per_sec": ops_per_sec,
        "theoretical_min_us_per_op": theoretical_min_us_per_op,
        "software_overhead_us": software_overhead_us,
        "utilization_pct": utilization_pct,
        "aggregate_mbps": aggregate_mbps,
    })
    return enriched


def pick_reference_sample(samples):
    ordered = sorted(samples, key=lambda item: item["us_per_op"])
    return ordered[len(ordered) // 2]


def summarize_metric(samples, key):
    values = [item[key] for item in samples]
    return min(values), statistics.median(values), max(values)


def print_report(samples):
    ref = pick_reference_sample(samples)
    backend = ref["backend"]

    print("=" * 68)
    print("Реальная скорость SPI")
    print("=" * 68)
    print(f"Backend:               {backend}")
    print(f"Устройство:            {ref['device']}")
    print(f"Логическая операция:   {ref['logical_op_label']}")
    print(f"Прогонов:              {len(samples)}")
    print(f"Операций в прогоне:    {ref['count']}")
    print(f"Настроенная частота:   {ref['configured_clock_hz'] / 1e6:.2f} МГц")
    print(f"Тактируемо на операцию:{ref['clocked_bits_per_op']} бит")
    print()

    for index, item in enumerate(samples, start=1):
        extra = ""
        if item["aggregate_mbps"] is not None:
            extra = f", aggregate={item['aggregate_mbps']:.2f} Мбит/с"
        print(
            f"Прогон {index}: "
            f"{item['us_per_op']:.2f} мкс/оп, "
            f"{item['ops_per_sec']:.0f} оп/с, "
            f"SCK={item['effective_sck_mhz']:.2f} МГц, "
            f"util={item['utilization_pct']:.1f}%"
            f"{extra}"
        )

    us_min, us_med, us_max = summarize_metric(samples, "us_per_op")
    sck_min, sck_med, sck_max = summarize_metric(samples, "effective_sck_mhz")
    util_min, util_med, util_max = summarize_metric(samples, "utilization_pct")
    ops_min, ops_med, ops_max = summarize_metric(samples, "ops_per_sec")

    print()
    print("Итог по медианному прогону")
    print("-" * 68)
    print(f"Время на операцию:     {ref['us_per_op']:.2f} мкс")
    print(f"Операций в секунду:    {ref['ops_per_sec']:.0f}")
    print(f"Эффективный SCK:       {ref['effective_sck_mhz']:.2f} МГц")
    print(f"Утилизация шины:       {ref['utilization_pct']:.1f}%")
    print(f"Теоретический минимум: {ref['theoretical_min_us_per_op']:.2f} мкс/оп")
    print(f"Накладные расходы:     {ref['software_overhead_us']:.2f} мкс/оп")
    if ref["aggregate_mbps"] is not None:
        print(f"Aggregate throughput:  {ref['aggregate_mbps']:.2f} Мбит/с (TX+RX)")

    print()
    print("Разброс по прогонам")
    print("-" * 68)
    print(f"мкс/операция:          min={us_min:.2f}, median={us_med:.2f}, max={us_max:.2f}")
    print(f"операций/с:            min={ops_min:.0f}, median={ops_med:.0f}, max={ops_max:.0f}")
    print(f"эффективный SCK:       min={sck_min:.2f}, median={sck_med:.2f}, max={sck_max:.2f} МГц")
    print(f"утилизация шины:       min={util_min:.1f}, median={util_med:.1f}, max={util_max:.1f}%")
    print()
    print("Примечание: effective SCK считает только реально протактированные биты.")
    if backend == "spidev":
        print("Aggregate throughput для spidev дополнительно считает оба направления: MOSI + MISO.")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Тест реальной скорости SPI на /dev/intan или /dev/spidevX.Y."
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="Путь к устройству SPI. По умолчанию: auto (/dev/intan, если есть, иначе /dev/spidev1.1).",
    )
    parser.add_argument(
        "--count",
        type=positive_int,
        default=20000,
        help="Число логических операций в одном прогоне.",
    )
    parser.add_argument(
        "--repeat",
        type=positive_int,
        default=5,
        help="Число прогонов для оценки разброса.",
    )
    parser.add_argument(
        "--speed-hz",
        type=positive_int,
        default=10000000,
        help="Частота для spidev backend. Для /dev/intan игнорируется.",
    )
    parser.add_argument(
        "--payload-bytes",
        type=positive_int,
        default=4,
        help="Размер одного SPI transfer для spidev backend.",
    )
    parser.add_argument(
        "--transfers-per-op",
        type=positive_int,
        default=3,
        help="Сколько SPI transfer составляют одну логическую операцию в spidev backend.",
    )
    parser.add_argument(
        "--warmup-ops",
        type=positive_int,
        default=200,
        help="Число прогревочных операций для spidev backend.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    device = choose_device(args.device)
    if not os.path.exists(device):
        print(f"Устройство не найдено: {device}", file=sys.stderr)
        sys.exit(1)

    try:
        backend = infer_backend(device)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    samples = []
    try:
        for _ in range(args.repeat):
            if backend == "intan":
                raw = run_intan_once(device=device, count=args.count)
            else:
                raw = run_spidev_once(
                    device=device,
                    count=args.count,
                    speed_hz=args.speed_hz,
                    payload_bytes=args.payload_bytes,
                    transfers_per_op=args.transfers_per_op,
                    warmup_ops=args.warmup_ops,
                )
            samples.append(enrich_measurement(raw))
    except PermissionError as error:
        print(f"Недостаточно прав для доступа к {device}: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"Ошибка работы с {device}: {error}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    print_report(samples)


if __name__ == "__main__":
    main()
