# Intan RHS2116 SPI Kernel Driver

Драйвер ядра Linux для работы с чипом Intan RHS2116 через SPI. Создаёт символьное устройство `/dev/intan` для доступа из userspace.

## Требования

- Orange Pi Zero 2W (или совместимая плата на H616/H618)
- Ядро 6.1.31-sun50iw9 (заголовки установлены)
- Intan RHS2116 подключён к SPI1, chip select 1
- GPIO 226 (PH2) для питания Intan — управляется отдельно (Python-скрипт или gpiod)

## Сборка

```bash
cd driver
make
```

Сборка overlay (device tree):

```bash
make dtbo
# или
make sun50i-h616-intan.dtbo
```

## Установка

### 1. Установка kernel модуля

```bash
sudo make install
# или вручную:
sudo cp intan_spi.ko /lib/modules/$(uname -r)/
sudo depmod -a
```

### 2. Настройка Device Tree Overlay

**Важно:** Overlay Intan **заменяет** spidev на SPI1 CS1. Не загружайте оба одновременно.

Вариант A — добавить в автозагрузку:

```bash
sudo make install-overlay
```

Затем отредактировать `/boot/orangepiEnv.txt`:

```
overlays=... sun50i-h616-intan
```

Если ранее использовали `sun50i-h616-spi1-cs1-spidev` — **замените** его на `sun50i-h616-intan`.

Вариант B — ручная установка overlay:

```bash
sudo cp sun50i-h616-intan.dtbo /boot/dtb-6.1.31-sun50iw9/allwinner/overlay/
# Добавить sun50i-h616-intan в overlays в /boot/orangepiEnv.txt
```

### 3. Загрузка модуля

После перезагрузки (для применения overlay) или вручную:

```bash
sudo modprobe intan_spi
```

Проверка:

```bash
ls -la /dev/intan
dmesg | tail -5
# Ожидается: "Intan RHS2116 (Chip ID 0x0020), /dev/intan"
```

## Использование

### Чтение Chip ID (регистр 255)

```bash
# Прочитать 2 байта
sudo xxd -l 2 /dev/intan
# Ожидается: 0020 (32 в hex — Chip ID RHS2116)
```

### Запись RAW команды (4 байта)

Формат команды Intan: `[byte0, reg, val_hi, val_low]`

- READ:  `C0 xx 00 00`
- WRITE: `8x xx yy zz` (x = U/M флаги, yyzz = 16-bit value)

Пример через Python:

```python
with open('/dev/intan', 'rb') as f:
    data = f.read(2)  # Chip ID
    chip_id = int.from_bytes(data, 'big')
    print(f"Chip ID: 0x{chip_id:04X}")
```

## Ускорение Python-скриптов

При использовании драйвера (`/dev/intan`) скрипты **автоматически** работают в 3× быстрее:
- 1 syscall вместо 3 на каждую операцию READ/WRITE/CONVERT
- `stimulate_channel0.py` и `intan_udp_recorder.py` выбирают `/dev/intan`, если он существует

IOCTL (для расширенной разработки):
- `INTAN_IOC_READ_REG` — чтение регистра
- `INTAN_IOC_WRITE_REG` — запись регистра
- `INTAN_IOC_CONVERT` — CONVERT (получение ADC)
- `INTAN_IOC_BENCHMARK` — бенчмарк: `sudo python3 spi_benchmark.py`

### Тест реальной скорости SPI

Для замера не только "configured MHz", но и фактической скорости по тактам
SCK, времени на одну логическую операцию и утилизации шины используйте:

```bash
# Автовыбор: /dev/intan, если доступен, иначе /dev/spidev1.1
python3 spi_real_speed_test.py

# Явный замер через драйвер /dev/intan
python3 spi_real_speed_test.py --device /dev/intan --count 20000 --repeat 5

# Явный замер через spidev на 10 МГц
python3 spi_real_speed_test.py --device /dev/spidev1.1 --speed-hz 10000000
```

Для `/dev/intan` одна логическая операция соответствует одному READ регистра
(3 SPI transfer по 4 байта = 96 тактов). Для `spidev` операция настраивается
параметрами `--payload-bytes` и `--transfers-per-op`.

## Файлы

| Файл | Описание |
|------|----------|
| `intan_spi.c` | Исходный код драйвера |
| `spi_real_speed_test.py` | Реальный замер effective SCK, latency и утилизации SPI |
| `Makefile` | Сборка модуля и overlay |
| `sun50i-h616-intan.dts` | Device tree overlay (исходник) |
| `sun50i-h616-intan.dtbo` | Скомпилированный overlay |

## Отладка

```bash
# Проверка загрузки модуля
lsmod | grep intan

# Логи ядра
dmesg | grep -i intan

# Проверка SPI устройств
ls -la /sys/bus/spi/devices/
```

## Лицензия

GPL v2
