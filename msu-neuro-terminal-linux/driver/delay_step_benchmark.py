#!/usr/bin/env python3
"""
Бенчмарк нового DELAY-шага против старого READ 255.

Скрипт сравнивает:
- delay_step: один короткий SPI transfer на шаг DELAY
- read_reg_255: старый полный READ регистра 255 (3 x 4 B SPI)

Поддерживаемые backend:
- /dev/intan
- /dev/spidevX.Y

Примеры:
  python3 delay_step_benchmark.py
  python3 delay_step_benchmark.py --device /dev/intan --count 30000 --repeat 7
  python3 delay_step_benchmark.py --device /dev/spidev1.1 --speed-hz 10000000
"""

import argparse
import os
import statistics
import sys
import time

from intan_driver import IntanDriver

try:
    import spidev
except ImportError:
    spidev = None


DEFAULT_INTAN_DEVICE = "/dev/intan"
DEFAULT_SPIDEV_DEVICE = "/dev/spidev1.1"
INTAN_CONFIGURED_CLOCK_HZ = 25_000_000
DELAY_STEP_CLOCKED_BYTES = 4
READ255_CLOCKED_BYTES = 12
DELAY_STEP_CMD = [0xC0, 0xFF, 0x00, 0x00]
DUMMY_CMD = [0x00, 0x00, 0x00, 0x00]


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
    name = os.path.basename(device)
    if name == "intan":
        return "intan"
    if name.startswith("spidev") and "." in name:
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


class SpidevDelayBench:
    def __init__(self, device, speed_hz):
        if spidev is None:
            raise RuntimeError(
                "Модуль spidev не установлен. Установите python3-spidev "
                "или используйте backend /dev/intan."
            )
        self.device = device
        self.speed_hz = speed_hz
        self.spi = None

    def open(self):
        bus, dev = parse_spidev_path(self.device)
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.max_speed_hz = self.speed_hz
        self.spi.mode = 0
        self.spi.bits_per_word = 8

    def close(self):
        if self.spi is not None:
            self.spi.close()
            self.spi = None

    def delay_step(self):
        self.spi.xfer2(DELAY_STEP_CMD)

    def read_reg_255(self):
        self.spi.xfer2(DELAY_STEP_CMD)
        self.spi.xfer2(DUMMY_CMD)
        self.spi.xfer2(DUMMY_CMD)


def run_operation(bench, op_name, count, warmup):
    action = getattr(bench, op_name)
    for _ in range(warmup):
        action()

    t0 = time.perf_counter()
    for _ in range(count):
        action()
    t1 = time.perf_counter()
    return t1 - t0


def make_measurement(backend, device, configured_clock_hz, op_name, count, elapsed_s):
    clocked_bytes = DELAY_STEP_CLOCKED_BYTES if op_name == "delay_step" else READ255_CLOCKED_BYTES
    clocked_bits = clocked_bytes * 8
    total_clocked_bits = count * clocked_bits
    effective_sck_hz = total_clocked_bits / max(elapsed_s, 1e-12)
    us_per_op = elapsed_s / max(count, 1) * 1e6
    ops_per_sec = count / max(elapsed_s, 1e-12)
    theoretical_min_us = (clocked_bits / configured_clock_hz) * 1e6
    return {
        "backend": backend,
        "device": device,
        "operation": op_name,
        "count": count,
        "elapsed_s": elapsed_s,
        "configured_clock_hz": configured_clock_hz,
        "clocked_bits_per_op": clocked_bits,
        "effective_sck_mhz": effective_sck_hz / 1e6,
        "utilization_pct": (effective_sck_hz / configured_clock_hz) * 100.0,
        "us_per_op": us_per_op,
        "ops_per_sec": ops_per_sec,
        "theoretical_min_us_per_op": theoretical_min_us,
        "software_overhead_us": us_per_op - theoretical_min_us,
    }


def run_intan_benchmark(device, count, repeat, warmup):
    samples = {"delay_step": [], "read_reg_255": []}
    for _ in range(repeat):
        bench = IntanDriver(device)
        bench.open()
        try:
            elapsed = run_operation(bench, "delay_step", count, warmup)
            samples["delay_step"].append(
                make_measurement("intan", device, INTAN_CONFIGURED_CLOCK_HZ, "delay_step", count, elapsed)
            )
            elapsed = run_operation(bench, "read_reg_255", count, warmup)
            samples["read_reg_255"].append(
                make_measurement("intan", device, INTAN_CONFIGURED_CLOCK_HZ, "read_reg_255", count, elapsed)
            )
        finally:
            bench.close()
    return samples


def run_spidev_benchmark(device, speed_hz, count, repeat, warmup):
    samples = {"delay_step": [], "read_reg_255": []}
    for _ in range(repeat):
        bench = SpidevDelayBench(device, speed_hz)
        bench.open()
        try:
            elapsed = run_operation(bench, "delay_step", count, warmup)
            samples["delay_step"].append(
                make_measurement("spidev", device, speed_hz, "delay_step", count, elapsed)
            )
            elapsed = run_operation(bench, "read_reg_255", count, warmup)
            samples["read_reg_255"].append(
                make_measurement("spidev", device, speed_hz, "read_reg_255", count, elapsed)
            )
        finally:
            bench.close()
    return samples


def median_sample(samples):
    ordered = sorted(samples, key=lambda item: item["us_per_op"])
    return ordered[len(ordered) // 2]


def metric_range(samples, key):
    values = [item[key] for item in samples]
    return min(values), statistics.median(values), max(values)


def print_operation_report(title, samples):
    ref = median_sample(samples)
    us_min, us_med, us_max = metric_range(samples, "us_per_op")
    ops_min, ops_med, ops_max = metric_range(samples, "ops_per_sec")
    sck_min, sck_med, sck_max = metric_range(samples, "effective_sck_mhz")

    print(title)
    print("-" * 72)
    print(f"Тактируемо на операцию: {ref['clocked_bits_per_op']} бит")
    print(f"Медиана:                {ref['us_per_op']:.2f} мкс/оп")
    print(f"Операций в секунду:     {ref['ops_per_sec']:.0f}")
    print(f"Effective SCK:          {ref['effective_sck_mhz']:.2f} МГц")
    print(f"Утилизация шины:        {ref['utilization_pct']:.1f}%")
    print(f"Теоретический минимум:  {ref['theoretical_min_us_per_op']:.2f} мкс/оп")
    print(f"Накладные расходы:      {ref['software_overhead_us']:.2f} мкс/оп")
    print(f"Разброс мкс/оп:         min={us_min:.2f}, median={us_med:.2f}, max={us_max:.2f}")
    print(f"Разброс оп/с:           min={ops_min:.0f}, median={ops_med:.0f}, max={ops_max:.0f}")
    print(f"Разброс eff SCK:        min={sck_min:.2f}, median={sck_med:.2f}, max={sck_max:.2f} МГц")
    print()


def print_comparison(samples_by_op):
    delay_ref = median_sample(samples_by_op["delay_step"])
    read_ref = median_sample(samples_by_op["read_reg_255"])

    latency_speedup = read_ref["us_per_op"] / max(delay_ref["us_per_op"], 1e-12)
    ops_speedup = delay_ref["ops_per_sec"] / max(read_ref["ops_per_sec"], 1e-12)

    print("Сравнение")
    print("-" * 72)
    print(f"Новый DELAY-шаг быстрее READ 255 по latency:  x{latency_speedup:.2f}")
    print(f"Новый DELAY-шаг быстрее READ 255 по ops/sec:  x{ops_speedup:.2f}")
    print()
    print("Оценка времени DELAY N")
    print("-" * 72)
    for steps in (1, 10, 100, 1000):
        total_us = delay_ref["us_per_op"] * steps
        if total_us < 1000.0:
            human = f"{total_us:.1f} мкс"
        else:
            human = f"{total_us / 1000.0:.3f} мс"
        print(f"DELAY {steps:4d}  ~= {human}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Сравнение нового delay_step и старого READ 255 по SPI."
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
        help="Число операций в одном прогоне.",
    )
    parser.add_argument(
        "--repeat",
        type=positive_int,
        default=5,
        help="Число прогонов.",
    )
    parser.add_argument(
        "--warmup",
        type=positive_int,
        default=200,
        help="Число прогревочных операций перед замером.",
    )
    parser.add_argument(
        "--speed-hz",
        type=positive_int,
        default=10_000_000,
        help="Частота для spidev backend. Для /dev/intan игнорируется.",
    )
    return parser


def main():
    args = build_parser().parse_args()

    device = choose_device(args.device)
    if not os.path.exists(device):
        print(f"Устройство не найдено: {device}", file=sys.stderr)
        sys.exit(1)

    try:
        backend = infer_backend(device)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    try:
        if backend == "intan":
            samples = run_intan_benchmark(device, args.count, args.repeat, args.warmup)
            configured_clock_hz = INTAN_CONFIGURED_CLOCK_HZ
        else:
            samples = run_spidev_benchmark(device, args.speed_hz, args.count, args.repeat, args.warmup)
            configured_clock_hz = args.speed_hz
    except PermissionError as error:
        print(f"Недостаточно прав для доступа к {device}: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"Ошибка работы с {device}: {error}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    print("=" * 72)
    print("Бенчмарк DELAY-шага")
    print("=" * 72)
    print(f"Backend:               {backend}")
    print(f"Устройство:            {device}")
    print(f"Настроенная частота:   {configured_clock_hz / 1e6:.2f} МГц")
    print(f"Прогонов:              {args.repeat}")
    print(f"Операций в прогоне:    {args.count}")
    print()

    print_operation_report("Новый DELAY-шаг: single SPI transfer", samples["delay_step"])
    print_operation_report("Старый подход: READ 255 (3 x 4 B SPI)", samples["read_reg_255"])
    print_comparison(samples)


if __name__ == "__main__":
    main()
