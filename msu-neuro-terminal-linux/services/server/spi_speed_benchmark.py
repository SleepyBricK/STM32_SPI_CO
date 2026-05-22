#!/usr/bin/env python3
"""
Бенчмарк реальной скорости SPI на Orange Pi Zero 2W.
Измеряет эффективную пропускную способность при разных размерах пакетов и настройках.
Поддерживает SPI_IOC_MESSAGE (batch) для снижения накладных расходов.
"""

import time
import sys
import os
import struct
import ctypes
import fcntl

try:
    import spidev
except ImportError:
    print("Ошибка: Модуль spidev не установлен.")
    print("Установите: sudo apt-get install python3-spidev")
    sys.exit(1)

# --- SPI_IOC_MESSAGE batch (через ioctl) ---
O_RDWR = 2
SPI_IOC_MAGIC = ord('k')
# Linux _IOW: (1<<30) | (size<<16) | (type<<8) | nr
def _SPI_IOC_MESSAGE(n):
    size = n * ctypes.sizeof(_SpiIocTransfer)
    if size >= (1 << 14):  # _IOC_SIZEBITS limit
        return 0
    return (1 << 30) | (size << 16) | (SPI_IOC_MAGIC << 8) | 0

# Известные константы (spidev, Linux asm-generic)
_SPI_IOC_WR_MODE = 0x40016b01
_SPI_IOC_WR_MAX_SPEED_HZ = 0x40046b04
_SPI_IOC_WR_BITS_PER_WORD = 0x40036b03
_SPI_IOC_MESSAGE_1 = 0x40206b00   # 1×32 bytes
# 20×32=640: (1<<30)|(640<<16)|(0x6b<<8)|0
_SPI_IOC_MESSAGE_20 = 0x40286b00

class _SpiIocTransfer(ctypes.Structure):
    _fields_ = [
        ('tx_buf', ctypes.c_ulonglong),
        ('rx_buf', ctypes.c_ulonglong),
        ('len', ctypes.c_uint),
        ('speed_hz', ctypes.c_uint),
        ('delay_usecs', ctypes.c_ushort),
        ('bits_per_word', ctypes.c_ubyte),
        ('cs_change', ctypes.c_ubyte),
        ('tx_nbits', ctypes.c_ubyte),
        ('rx_nbits', ctypes.c_ubyte),
        ('word_delay_usecs', ctypes.c_ubyte),
        ('pad', ctypes.c_ubyte),
    ]

def spi_batch_20_init(device, speed_hz):
    """Открывает SPI, настраивает. Возвращает (fd, transfers). Буферы хранятся в структуре."""
    fd = os.open(device, O_RDWR)
    fcntl.ioctl(fd, _SPI_IOC_WR_MODE, struct.pack('B', 0))
    fcntl.ioctl(fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack('I', speed_hz))
    fcntl.ioctl(fd, _SPI_IOC_WR_BITS_PER_WORD, struct.pack('B', 8))
    conv_63 = (0x00, 0x3F, 0x00, 0x00)
    conv_0 = (0x00, 0x00, 0x00, 0x00)
    cmds = [conv_63, conv_63, conv_0] + [conv_63] * 17
    tx_arr = [ctypes.create_string_buffer(bytes(c), 4) for c in cmds]
    rx_arr = [bytearray(4) for _ in range(20)]
    transfers = (_SpiIocTransfer * 20)()
    for i in range(20):
        transfers[i].tx_buf = ctypes.addressof(tx_arr[i])
        transfers[i].rx_buf = ctypes.addressof(ctypes.c_char.from_buffer(rx_arr[i]))
        transfers[i].len = 4
        transfers[i].speed_hz = 0
        transfers[i].delay_usecs = 0
        transfers[i].bits_per_word = 0
        transfers[i].cs_change = 1
        transfers[i].tx_nbits = 0
        transfers[i].rx_nbits = 0
        transfers[i].word_delay_usecs = 0
        transfers[i].pad = 0
    return fd, (tx_arr, rx_arr, transfers)


def spi_batch_partial_init(device, speed_hz, batch_n):
    """Fallback: кадр из 20 tx разбит на вызовы MESSAGE(n), n<=batch_n."""
    fd = os.open(device, O_RDWR)
    fcntl.ioctl(fd, _SPI_IOC_WR_MODE, struct.pack('B', 0))
    fcntl.ioctl(fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack('I', speed_hz))
    fcntl.ioctl(fd, _SPI_IOC_WR_BITS_PER_WORD, struct.pack('B', 8))
    conv_63 = (0x00, 0x3F, 0x00, 0x00)
    conv_0 = (0x00, 0x00, 0x00, 0x00)
    cmds_full = [conv_63, conv_63, conv_0] + [conv_63] * 17
    batches = []
    keep_bufs = []  # держим ссылки на буферы, чтобы они не освободились
    for i in range(0, 20, batch_n):
        n = min(batch_n, 20 - i)
        chunk = cmds_full[i:i + n]
        req = _SPI_IOC_MESSAGE(n) or (0x40206b00 if n == 1 else 0)
        tx_arr = [ctypes.create_string_buffer(bytes(c), 4) for c in chunk]
        rx_arr = [bytearray(4) for _ in chunk]
        keep_bufs.append((tx_arr, rx_arr))
        t = (_SpiIocTransfer * n)()
        for j in range(n):
            t[j].tx_buf = ctypes.addressof(tx_arr[j])
            t[j].rx_buf = ctypes.addressof(ctypes.c_char.from_buffer(rx_arr[j]))
            t[j].len = 4
            t[j].cs_change = 1
        batches.append((req, t))
    return fd, batches, keep_bufs, len(batches)


def _run_partial_batch(fd, batches):
    for r, t in batches:
        fcntl.ioctl(fd, r, t)


def run_benchmark(device="/dev/spidev1.1", max_speed_hz=25000000, chunk_size=4, n_transfers=10000):
    """
    Выполняет серию SPI-транзакций и измеряет время.
    
    chunk_size=4 — типичный размер команды Intan (32 бита)
    """
    if not os.path.exists(device):
        print(f"Ошибка: {device} не найден. Проверьте, включён ли SPI.")
        return None
    
    bus, dev = device.replace("/dev/spidev", "").split(".")
    spi = spidev.SpiDev()
    spi.open(int(bus), int(dev))
    spi.max_speed_hz = max_speed_hz
    spi.mode = 0
    spi.bits_per_word = 8
    
    data = [0x00] * chunk_size
    
    # Прогрев
    for _ in range(100):
        spi.xfer2(data)
    
    # Замер
    t0 = time.perf_counter()
    for _ in range(n_transfers):
        spi.xfer2(data)
    t1 = time.perf_counter()
    
    spi.close()
    
    elapsed = t1 - t0
    total_bytes = n_transfers * chunk_size * 2  # xfer2 — full duplex: отправка + приём
    bytes_per_sec = total_bytes / elapsed
    bits_per_sec = bytes_per_sec * 8
    transfers_per_sec = n_transfers / elapsed
    us_per_transfer = elapsed / n_transfers * 1e6
    
    return {
        "elapsed": elapsed,
        "n_transfers": n_transfers,
        "chunk_size": chunk_size,
        "total_bytes": total_bytes,
        "bytes_per_sec": bytes_per_sec,
        "bits_per_sec": bits_per_sec,
        "transfers_per_sec": transfers_per_sec,
        "us_per_transfer": us_per_transfer,
        "configured_mhz": max_speed_hz / 1e6,
    }


def _run_batch_benchmark(device, speeds):
    """Batch benchmark — запускается первым, до spidev."""
    print("\n" + "=" * 60)
    print("record_channels: SPI_IOC_MESSAGE(20) — 1 ioctl/кадр (batch, до spidev)")
    print("=" * 60)

    req20 = _SPI_IOC_MESSAGE(20)
    if req20 == 0 or ctypes.sizeof(_SpiIocTransfer) != 32:
        req20 = _SPI_IOC_MESSAGE_20
    n_frames_batch = 5000

    max_batch_n = 0
    probe_fd = -1
    probe_transfers = None
    probe_req = 0
    for n in [1, 2, 4, 8, 10, 16, 20]:
        req = _SPI_IOC_MESSAGE(n) if n * 32 < (1 << 14) else 0
        if req == 0:
            break
        fd = -1
        try:
            fd = os.open(device, O_RDWR)
            fcntl.ioctl(fd, _SPI_IOC_WR_MODE, struct.pack('B', 0))
            fcntl.ioctl(fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack('I', 10_000_000))
            cmds = [(0x00, 0x3F, 0x00, 0x00)] * n
            tx_arr = [ctypes.create_string_buffer(bytes(c), 4) for c in cmds]
            rx_arr = [bytearray(4) for _ in range(n)]
            t = (_SpiIocTransfer * n)()
            for i in range(n):
                t[i].tx_buf = ctypes.addressof(tx_arr[i])
                t[i].rx_buf = ctypes.addressof(ctypes.c_char.from_buffer(rx_arr[i]))
                t[i].len = 4
                t[i].cs_change = 1
            fcntl.ioctl(fd, req, t)
            max_batch_n = n
            if n == 20:
                probe_fd = fd
                probe_transfers = (tx_arr, rx_arr, t)
                probe_req = req
                fd = -1
        except OSError:
            break
        finally:
            if fd >= 0:
                os.close(fd)
    if max_batch_n == 0:
        print("  SPI_IOC_MESSAGE недоступен (драйвер не поддерживает)")
    elif max_batch_n < 20:
        print(f"  Драйвер: MESSAGE(1..{max_batch_n}) — используем {20 // max_batch_n}× MESSAGE({max_batch_n}) на кадр")
    else:
        print(f"  Проба: MESSAGE(20) OK, используем 1 ioctl/кадр")

    # При max_batch_n>=20 используем один fd для всех скоростей — переоткрытие ломает ioctl на Allwinner
    for speed in speeds:
        if max_batch_n == 0:
            break
        try:
            if max_batch_n >= 20:
                if probe_fd >= 0 and probe_transfers is not None:
                    fd = probe_fd
                    _, _, transfers = probe_transfers
                    fcntl.ioctl(fd, _SPI_IOC_WR_MAX_SPEED_HZ, struct.pack('I', speed))
                    n_ioctl_per_frame = 1
                    close_fd = False
                else:
                    fd, (tx_arr, rx_arr, transfers) = spi_batch_20_init(device, speed)
                    n_ioctl_per_frame = 1
                    close_fd = True
            else:
                fd, batches, _, n_ioctl_per_frame = spi_batch_partial_init(device, speed, max_batch_n)
                transfers = batches
                close_fd = True
            try:
                if n_ioctl_per_frame == 1:
                    req = probe_req if (probe_fd >= 0 and fd == probe_fd) else req20
                    fcntl.ioctl(fd, req, transfers)
                    t0 = time.perf_counter()
                    for _ in range(n_frames_batch):
                        fcntl.ioctl(fd, req, transfers)
                    t1 = time.perf_counter()
                else:
                    _run_partial_batch(fd, batches)
                    t0 = time.perf_counter()
                    for _ in range(n_frames_batch):
                        _run_partial_batch(fd, batches)
                    t1 = time.perf_counter()
            finally:
                if close_fd:
                    os.close(fd)
            us_per_frame = (t1 - t0) / n_frames_batch * 1e6
            fps = 1e6 / us_per_frame
            print(f"  {speed/1e6:.0f} МГц: {us_per_frame:.1f} мкс/кадр → ~{fps:.0f} кадр/с, ~{fps:.0f} сэмпл/с на канал (×16 = {fps*16:.0f} всего) [{n_ioctl_per_frame} ioctl/кадр]")
        except (OSError, IOError) as e:
            errmsg = str(e)
            if "25" in errmsg or "Inappropriate" in errmsg:
                errmsg += " (проба: MESSAGE({}) OK, но ioctl в бенчмарке падает)".format(max_batch_n)
            print(f"  {speed/1e6:.0f} МГц: ошибка batch — {errmsg}")
            try:
                fd2, batches, _, n_ioctl = spi_batch_partial_init(device, speed, 1)
                _run_partial_batch(fd2, batches)
                t0 = time.perf_counter()
                for _ in range(n_frames_batch):
                    _run_partial_batch(fd2, batches)
                t1 = time.perf_counter()
                os.close(fd2)
                us_per_frame = (t1 - t0) / n_frames_batch * 1e6
                fps = 1e6 / us_per_frame
                print(f"  {speed/1e6:.0f} МГц: fallback MESSAGE(1): {us_per_frame:.1f} мкс/кадр → ~{fps:.0f} кадр/с ({n_ioctl} ioctl/кадр)")
            except (OSError, IOError) as e2:
                print(f"  {speed/1e6:.0f} МГц: fallback MESSAGE(1) тоже падает: {e2}")

    if probe_fd >= 0:
        try:
            os.close(probe_fd)
        except OSError:
            pass


def main():
    device = os.environ.get("SPI_DEVICE", "/dev/spidev1.1")
    
    if not os.path.exists(device):
        print(f"Устройство {device} не найдено.")
        print("Убедитесь, что SPI включён: armbian-config -> System -> Hardware -> spi-spidev")
        sys.exit(1)
    
    print("=" * 60)
    print("Бенчмарк SPI — Orange Pi Zero 2W")
    print("=" * 60)
    print(f"Устройство: {device}\n")
    
    speeds = [1_000_000, 5_000_000, 10_000_000, 25_000_000]
    chunk_sizes = [4, 16, 64, 256]  # 4 байта = команда Intan

    # Сначала batch (до spidev) — проверка, не портит ли spidev состояние устройства
    _run_batch_benchmark(device, speeds)

    for speed in speeds:
        print(f"\n--- Настроенная скорость: {speed/1e6:.0f} МГц ---")
        
        for chunk in chunk_sizes:
            n = max(10000, 50000 // chunk)  # больше итераций для маленьких пакетов
            r = run_benchmark(device, max_speed_hz=speed, chunk_size=chunk, n_transfers=n)
            if r is None:
                continue
            
            effective_mbps = r["bytes_per_sec"] * 8 / 1e6
            util = 100 * effective_mbps / (speed / 1e6) if speed else 0
            
            print(f"  Пакет {chunk:3d} байт: "
                  f"{r['bytes_per_sec']/1e6:.2f} МБ/с "
                  f"({effective_mbps:.1f} Мбит/с), "
                  f"{r['us_per_transfer']:.1f} мкс/транзакция "
                  f"({r['transfers_per_sec']/1000:.1f} тыс/с)")
            if chunk == 4:
                print(f"           Утилизация шины: ~{util:.0f}% (остальное — накладные расходы)")
    
    # Детальный замер для типичного сценария Intan (3 × 4 байта на CONVERT)
    print("\n" + "=" * 60)
    print("Сценарий Intan: 3 транзакции по 4 байта (один CONVERT)")
    print("=" * 60)
    
    for speed in speeds:
        # Симулируем один цикл ADC: 3 xfer по 4 байта
        bus, dev = device.replace("/dev/spidev", "").split(".")
        spi = spidev.SpiDev()
        spi.open(int(bus), int(dev))
        spi.max_speed_hz = speed
        spi.mode = 0
        
        cmd = [0x00] * 4
        n_cycles = 5000
        
        t0 = time.perf_counter()
        for _ in range(n_cycles):
            spi.xfer2(cmd)
            spi.xfer2(cmd)
            spi.xfer2(cmd)
        t1 = time.perf_counter()
        
        spi.close()
        
        us_per_cycle = (t1 - t0) / n_cycles * 1e6
        max_sps = 1e6 / us_per_cycle  # сэмплов в секунду
        
        print(f"  {speed/1e6:.0f} МГц: {us_per_cycle:.1f} мкс/цикл → макс. ~{max_sps:.0f} сэмпл/с")
    
    # Сценарий с 1 tx на CONVERT (новый оптимизированный режим)
    print("\n" + "=" * 60)
    print("Сценарий Intan: 1 транзакция на CONVERT (pipeline в GUI)")
    print("=" * 60)
    
    for speed in speeds:
        bus, dev = device.replace("/dev/spidev", "").split(".")
        spi = spidev.SpiDev()
        spi.open(int(bus), int(dev))
        spi.max_speed_hz = speed
        spi.mode = 0
        cmd = [0x00] * 4
        n_cycles = 15000
        t0 = time.perf_counter()
        for _ in range(n_cycles):
            spi.xfer2(cmd)
        t1 = time.perf_counter()
        spi.close()
        us_per_sample = (t1 - t0) / n_cycles * 1e6
        max_sps = 1e6 / us_per_sample
        print(f"  {speed/1e6:.0f} МГц: {us_per_sample:.1f} мкс/сэмпл → макс. ~{max_sps:.0f} сэмпл/с")
    
    # Полный кадр record_channels (20 tx на 16 каналов, без sleep) — реальный режим записи
    print("\n" + "=" * 60)
    print("record_channels: 20 tx/кадр × 16 каналов, без sleep (макс. скорость)")
    print("=" * 60)
    
    conv_63 = [0x00, 0x3F, 0x00, 0x00]
    conv_0 = [0x00, 0x00, 0x00, 0x00]
    n_frames = 3000
    
    for speed in speeds:
        bus, dev = device.replace("/dev/spidev", "").split(".")
        spi = spidev.SpiDev()
        spi.open(int(bus), int(dev))
        spi.max_speed_hz = speed
        spi.mode = 0
        t0 = time.perf_counter()
        for _ in range(n_frames):
            spi.xfer2(conv_63)
            spi.xfer2(conv_63)
            spi.xfer2(conv_0)
            for _ in range(17):
                spi.xfer2(conv_63)
        t1 = time.perf_counter()
        spi.close()
        us_per_frame = (t1 - t0) / n_frames * 1e6
        fps = 1e6 / us_per_frame
        sps_per_ch = fps  # один сэмпл на канал за кадр
        print(f"  {speed/1e6:.0f} МГц: {us_per_frame:.1f} мкс/кадр → ~{fps:.0f} кадр/с, ~{sps_per_ch:.0f} сэмпл/с на канал (×16 = {fps*16:.0f} всего)")
    


if __name__ == "__main__":
    main()
