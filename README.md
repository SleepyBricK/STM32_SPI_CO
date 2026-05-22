# WeActSTM32H743 — прошивка для STM32H743 + USB3300 ULPI + Intan RHS2116

Прошивка для пользовательской платы на **STM32H743VIT6** (Cortex-M7, LQFP100). Изначально — порт отладочной платы **WeAct STM32H743**; сейчас адаптирована под плату с **HSE 8 MHz**, **USB3300 ULPI** (USB 2.0 High Speed) и опциональным чипом **Intan RHS2116** по SPI2.

**Два канала связи с хостом:**

| Канал | Назначение |
|-------|------------|
| **USART1** (115200, PB6/PB7) | Интерактивный CLI: инициализация Intan, стимуляция, бенчмарки SPI |
| **USB HS vendor bulk** (`0483:5741`) | Высокоскоростной обмен с Orange Pi / Mac / Linux через `libusb` / PyUSB |

Логика протокола Intan согласована с референсным проектом [`msu-neuro-terminal-linux/`](msu-neuro-terminal-linux/) (драйвер ядра `driver/intan_spi.c`, Python-сервисы).

> **Для AI-агентов в Cursor:** краткий оперативный контекст — в [`AGENTS.md`](AGENTS.md). Этот README — полное руководство для человека.

---

## Содержание

1. [Возможности](#возможности)
2. [Железо и распиновка](#железо-и-распиновка)
3. [Тактирование](#тактирование)
4. [Структура репозитория](#структура-репозитория)
5. [Требования и сборка](#требования-и-сборка)
6. [Прошивка и первый запуск](#прошивка-и-первый-запуск)
7. [UART CLI](#uart-cli)
8. [USB vendor bulk](#usb-vendor-bulk)
9. [Host-утилиты (Python)](#host-утилиты-python)
10. [Intan RHS2116 — типовой сценарий](#intan-rhs2116--типовой-сценарий)
11. [Тесты скорости (ksps)](#тесты-скорости-ksps)
12. [Стимуляция](#стимуляция)
13. [Диагностика и типичные ошибки](#диагностика-и-типичные-ошибки)
14. [Эталон WorkingVER и ulpi-fw](#эталон-workingver-и-ulpi-fw)
15. [STM32CubeMX и регенерация](#stm32cubemx-и-регенерация)
16. [Справочные материалы](#справочные-материалы)

---

## Возможности

- **USB 2.0 High Speed** через внешний PHY **USB3300** (ULPI), класс **vendor-specific bulk** для минимального overhead.
- **Текстовые USB-команды** в main loop (не в USB IRQ): `PING`, `ECHO`, `ID`, `READ`, `CONVERT`, `STREAM`.
- **Бинарный поток `STREAM`**: до сотен ksps CONVERT через SPI DMA + TIM1 CS, samples — **16-bit little-endian** без заголовка.
- **UART CLI** с полным набором команд Intan: регистры, инициализация записи/стима, бенчмарки, паттерны стимуляции.
- **Режим без Intan** (`WITH_INTAN_HW=OFF`, по умолчанию): USB `PING`/`ECHO`/`HELP`, UART `PING`/`HELP` — для отладки USB до запайки чипа.
- **Ранний UART** до настройки PLL и метки отладки по всему boot path.
- **Late USB reconnect** после полной инициализации (важно после прошивки через ST-Link).

---

## Железо и распиновка

| Параметр | Значение |
|----------|----------|
| МК | STM32H743VIT6, LQFP100 |
| CMSIS/HAL | `STM32H743xx` |
| HSE | **8 MHz** (PH0/PH1) |
| LSE | **32.768 kHz** (PC14/PC15) — **опционально** (`BOARD_HAS_LSE`) |
| Отладка | PA13 SWDIO, PA14 SWCLK (**ST-Link**) |
| USB к хосту | **USB3300** — отдельный разъём/кабель, **не ST-Link** |

### SPI2 — Intan RHS2116

| Сигнал | Пин | Примечание |
|--------|-----|------------|
| SCK | PA9 | ≈25 Mbit/s, кадр **32 бита** |
| MISO | PB14 | |
| MOSI | PC1 | |
| CS | **PE11** | Программный или TIM1_CH2 (DMA-режим), активный **LOW** |

Инициализация SPI2 и bringup Intan выполняются **только** при `INTAN_HW_PRESENT=1` (`-DWITH_INTAN_HW=ON`).

### USART1 — CLI

| Сигнал | Пин | Параметры |
|--------|-----|-----------|
| TX | PB6 | 115200 8N1 → RX ПК |
| RX | PB7 | ← TX ПК |

### USB3300 ULPI

| ULPI | Пин STM32 |
|------|-----------|
| STP | PC0 |
| DIR | PC2_C |
| NXT | PC3_C |
| CLK | PA5 |
| D0–D7 | PA3, PB0, PB1, PB10–PB13, PB5 |

Инициализация: `Core/Src/usb3300_ulpi_hw.c` — задержка XTAL **10 ms**, **PLL3 → 48 MHz** для USB kernel clock, `DisableUSBReg` + ожидание `USB33RDY`, настройка GPIO ULPI (пины **не** должны оставаться в режиме ANALOG).

### USB device (к хосту)

| Параметр | Значение |
|----------|----------|
| VID:PID | **`0483:5741`** |
| Класс | Vendor-specific (interface 0) |
| Bulk OUT | **`0x01`** — команды от хоста |
| Bulk IN | **`0x81`** — ответы / поток samples |
| HS max packet | **512 bytes** |
| Строка продукта | `STM32H743 Intan HS Bulk` |

**Важно:** `USBD_VENDOR_BULK_Transmit()` принимает **не более 512 байт** за один вызов. Не отправлять 8192 B одним вызовом — это ломало enumeration.

---

## Тактирование

Источник правды для boot/USB: `SystemClock_Config()` в `Core/Src/main.c` (согласовано с **`WorkingVER/STM32H743/`**).

| PLL / шина | Формула (HSE 8 MHz) | Результат |
|------------|---------------------|-----------|
| PLL1 → SYSCLK | HSE / 4 × 240 / 2 | **240 MHz** |
| AHB (HCLK) | SYSCLK / 2 | **120 MHz** |
| PLL2P (SPI123) | HSE / 2 × 100 / 2 | **200 MHz** → SCK SPI2 ≈ **25 MHz** (/8) |
| PLL3 (USB) | HSE / 4 × 96 / 4 | **48 MHz** |

- Регулятор: **`PWR_REGULATOR_VOLTAGE_SCALE2`** (не VSCALE0/480 MHz без веской причины).
- **LSE не включается** в `SystemClock_Config` — только в `MX_RTC_Init`, если `BOARD_HAS_LSE=1`. Так UART/USB не зависят от наличия 32 kHz кварца при старте.

---

## Структура репозитория

```
WeActSTM32H743/
├── Core/
│   ├── Inc/                    # Заголовки HAL + intan_*, usb_*
│   └── Src/
│       ├── main.c              # Boot, clocks, main loop
│       ├── gpio.c              # GPIO (ULPI не в ANALOG)
│       ├── spi.c               # hspi2 (Intan)
│       ├── usart.c             # UART + early debug
│       ├── rtc.c               # RTC/LSE (опционально)
│       ├── usb3300_ulpi_hw.c   # USB3300 + PLL3
│       ├── usb_device.c        # USB stack, late reconnect
│       ├── usbd_conf.c         # PCD/ULPI callbacks (без UART!)
│       ├── usbd_vendor_bulk.c  # Bulk class
│       ├── usbd_desc.c         # VID/PID/descriptor
│       ├── intan_spi.c         # Протокол RHS2116
│       ├── intan_spi4_hw.c     # SPI2 через TXDR/RXDR (без HAL_TransmitReceive)
│       ├── intan_app.c         # INIT_RECORD, бенчи, стим-паттерны
│       ├── intan_uart_cli.c    # UART CLI
│       └── intan_usb_bulk.c    # USB-команды + STREAM
├── cmake/
│   ├── gcc-arm-none-eabi.cmake # Тулчейн ARM GCC
│   └── stm32cubemx/            # Источники CubeMX
├── tools/                      # Host Python (PyUSB)
├── ulpi-fw/                    # Минимальная ULPI-only прошивка (PID 5742)
├── WorkingVER/STM32H743/       # Эталон USB+UART (CDC 5740)
├── msu-neuro-terminal-linux/   # Референс Linux/Python (не нужен для сборки)
├── WeActSTM32H743.ioc          # STM32CubeMX
├── CMakeLists.txt
├── AGENTS.md                   # Краткий контекст для агентов
└── README.md                   # Этот файл
```

### Ключевые файлы по назначению

| Файл | Роль |
|------|------|
| `intan_spi.h` | Пины CS, `INTAN_HW_PRESENT`, API Intan |
| `intan_spi4_hw.c` | Низкоуровневый SPI2 (CFG1/2, 32-bit кадры) |
| `intan_usb_bulk.c` | Парсер USB-команд, `STREAM` через DMA |
| `intan_uart_cli.c` | Полный UART CLI + HELP |
| `usbd_conf.c` | FIFO TX0=0x40, TX1=0x100; callbacks без блокирующего UART |

---

## Требования и сборка

### Инструменты

| Инструмент | Назначение |
|------------|------------|
| **arm-none-eabi-gcc** | Кросс-компиляция (Cortex-M7, hard float) |
| **CMake** ≥ 3.22 | Система сборки |
| **STM32Cube FW_H7 V1.13.0** | Ожидается по пути `../Repository/STM32Cube_FW_H7_V1.13.0` (см. `CMakeLists.txt`) |
| **STM32CubeProgrammer** / OpenOCD | Прошивка по SWD |
| **Python 3** + **PyUSB** | Host-тесты USB |

На **macOS** обязательно указывать тулчейн явно (иначе подхватится AppleClang):

```bash
brew install arm-none-eabi-gcc cmake libusb
pip3 install pyusb
```

На **Linux** (Orange Pi и т.п.):

```bash
sudo apt install gcc-arm-none-eabi cmake libusb-1.0-0-dev
pip3 install pyusb
# при необходимости: udev-правила для VID 0483
```

### Опции CMake

| Опция | По умолчанию | Макрос | Назначение |
|-------|--------------|--------|------------|
| `WITH_INTAN_HW` | **OFF** | `INTAN_HW_PRESENT` | Intan RHS2116 запаян на SPI2 |
| `BOARD_HAS_LSE` | **OFF** | `BOARD_HAS_LSE` | Кварц 32.768 kHz на PC14/PC15 для RTC |

### Сборка (USB-only, без Intan)

```bash
cd /path/to/WeActSTM32H743
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

Артефакт: **`build/WeActSTM32H743.elf`**

### Сборка с Intan и LSE

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DBOARD_HAS_LSE=ON
cmake --build build
```

Release (опционально):

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

---

## Прошивка и первый запуск

### Прошивка через ST-Link (SWD)

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

Или через OpenOCD / IDE — главное: **после прошивки переподключить USB-кабель USB3300 к хосту** (ST-Link и USB3300 — разные интерфейсы).

### Последовательность инициализации (main)

1. `HAL_Init` → ранний UART: `EARLY`
2. `SystemClock_Config` → `CLK`
3. `MX_GPIO_Init`, `MX_USART1_UART_Init`
4. `USB3300_ULPI_HwInit` → `[ULPI]` метки
5. `USB_DEVICE_Init`
6. `MX_RTC_Init` (LSE только если `BOARD_HAS_LSE=1`)
7. При `INTAN_HW_PRESENT=1`: `MX_SPI2_Init`, `Intan_SPI_Init`, `Intan_ChipBringup`
8. `Intan_UART_CLI_Init` → **`INTAN_UART_READY`** + HELP
9. `USB_DEVICE_FinalizeAttach()` — late reconnect для хоста
10. Main loop: `Intan_UART_CLI_Process()` + `Intan_USB_Bulk_Process()` + `USB_DEVICE_PollEvents()`

### Типовый лог UART (115200, PB6)

```
EARLY
CLK
BOOT
[M] post MX_GPIO + MX_USART1
...
INTAN_UART_READY
OK CMDS: ...
```

**Префиксы отладки:**

| Префикс | Источник |
|---------|----------|
| `[M]` | `main.c` |
| `[U]` | `MX_USART1` / USART |
| `[R]` | RTC / LSE |
| `[S]` | SPI2 |
| `[I]` | Intan init |
| `[C]` | UART CLI |
| `[ULPI]` / `[USB]` | USB3300 / USB stack |

При фatal ошибке: **`!ERR_HANDLER`** и SOS на PB6 (если UART уже поднят).

### Быстрая проверка USB (без Intan)

```bash
python3 tools/usb_intan_cmd.py PING
# OK PONG

python3 tools/usb_intan_cmd.py ECHO hello
# OK ECHO hello

python3 -c "import usb.core; d=usb.core.find(idVendor=0x0483,idProduct=0x5741); print(d.speed)"
# 3 = USB 2.0 High Speed
```

В UART после подключения хоста ожидайте `[USB] host reset`, затем `dev_state=3` (CONFIGURED).

> На новых macOS `system_profiler SPUSBDataType` часто **не показывает** vendor-устройства — ориентируйтесь на **PyUSB + PING**.

---

## UART CLI

Подключение: **115200 8N1**, TX платы **PB6** → RX USB-UART адаптера.

Команды — одна строка, регистр важен для имени команды. Ответы: `OK ...` или `ERR ...`.

### Команды без Intan (`INTAN_HW_PRESENT=0`)

| Команда | Ответ | Описание |
|---------|-------|----------|
| `HELP` / `?` | Список | Справка |
| `PING` | `OK PONG` | Проверка UART |

Все прочие команды → `ERR no intan hw`.

### Команды с Intan (`WITH_INTAN_HW=ON`)

#### Идентификация и регистры

| Команда | Пример | Описание |
|---------|--------|----------|
| `ID` | `ID` | Chip ID (reg 255), raw32, проверка HI16 |
| `ROM` | `ROM` | ASCII из регистров 251–253 |
| `READ r` | `READ 255` | Чтение 16-bit значения регистра |
| `READRAW r` | `READRAW 255` | + полное 32-bit слово MISO |
| `WRITE r hex [u m]` | `WRITE 0 0x1234` | Запись регистра (флаги u/m опционально) |
| `CONVERT ch [h d]` | `CONVERT 0 0` | Одна выборка ADC (flags = DSP/DC) |

#### Инициализация

| Команда | Пример | Описание |
|---------|--------|----------|
| `INIT_RECORD [ksps]` | `INIT_RECORD 480` | Таблица bias/ADC для записи; default **480 kS/s** |
| `INIT_STIM` | `INIT_STIM` | Базовая инициализация стимуляции |
| `CLEAR_ADC` | `CLEAR_ADC` | Сброс ADC (даташит) |
| `CLEAR_COMP` | `CLEAR_COMP` | Сброс compliance monitor |

**Перед бенчмарками и STREAM рекомендуется:** `INIT_RECORD 480` (или нужная частота).

#### Спецификаторы каналов (стим)

- `ALL` или `*` — все каналы
- `0`, `2` — один канал
- `0-3` — диапазон
- `pol`: `0` = negative, `1` = positive

---

## USB vendor bulk

### Архитектура

```
Хост (PyUSB)                    STM32
    │  Bulk OUT 0x01  ────────►  IRQ: пакет в очередь
    │                              main loop: Intan_USB_Bulk_Process()
    │                              dispatch_usb_command()
    │  Bulk IN 0x81   ◄────────  текстовый OK/ERR или бинарный STREAM
```

- Команды **не** обрабатываются в USB ISR (иначе ломается enumeration).
- Текстовые ответы заканчиваются `\n`.
- **`STREAM`**: после команды устройство шлёт **только** бинарные данные (2×n байт), без текстового заголовка.

### USB-команды

| Команда | Intan нужен? | Описание |
|---------|--------------|----------|
| `HELP` / `?` | Нет | Список команд |
| `PING` | Нет | `OK PONG` |
| `ECHO text` | Нет | `OK ECHO text` |
| `ID` | Да | Chip ID |
| `READ r` | Да | Значение регистра |
| `READRAW r` | Да | + raw32 |
| `CONVERT ch [flags]` | Да | Одна выборка |
| `STREAM n [ch] [flags]` | Да | n samples, 16-bit LE bulk IN |

Примеры:

```bash
python3 tools/usb_intan_cmd.py HELP
python3 tools/usb_intan_cmd.py ID
python3 tools/usb_intan_cmd.py READ 255
python3 tools/usb_intan_cmd.py CONVERT 0 0
```

Без Intan:

```bash
python3 tools/usb_intan_cmd.py READ 255
# ERR no intan hw
```

### Формат STREAM

1. Хост пишет на OUT: `STREAM 50000 0 0\n`
2. Устройство выполняет `Intan_ConvertPipelineDmaTimCsRead` блоками до 4096 samples
3. На IN уходят пакеты по **≤256 samples** (512 B) за USB transfer
4. Payload: `uint16_t samples[n]` — **little-endian**

---

## Host-утилиты (Python)

Все скрипты в каталоге **`tools/`**, зависимость: **`pyusb`**, backend **libusb**.

| Скрипт | Назначение |
|--------|------------|
| `usb_intan_cmd.py` | Одна произвольная USB-команда |
| `usb_bulk_loopback.py` | Echo-тест (`ECHO`), проверка round-trip bulk |
| `usb_stream_bench.py` | Замер ksps / MB/s через `STREAM` |
| `usb_ulpi_test.py` | Тест для PID **5742** (см. `ulpi-fw/`) |

### usb_intan_cmd.py

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py READ 255
python3 tools/usb_intan_cmd.py --timeout-ms 5000 STREAM 1000 0 0
# STREAM через этот скрипт неудобен (бинарный ответ) — используйте usb_stream_bench.py

python3 tools/usb_intan_cmd.py --vid 0x0483 --pid 0x5741 ID
```

### usb_bulk_loopback.py

Проверка транспорта **без Intan**:

```bash
python3 tools/usb_bulk_loopback.py
python3 tools/usb_bulk_loopback.py --size 512
```

### usb_stream_bench.py

Полный end-to-end бенч (Intan + USB + DMA path):

```bash
python3 tools/usb_stream_bench.py -n 50000 --channel 0
python3 tools/usb_stream_bench.py -n 100000 --channel 63 --flags 0 --timeout-ms 10000
```

Вывод:

```
samples=50000 bytes=100000 elapsed=0.089123s
ksps_total=561.023
throughput=1.122 MB/s
first=0x1234 0x5678 ...
pyusb speed=3
```

- **`ksps_total`** — число команд CONVERT в секунду (тысячи samples/s на уровне протокола).
- **`pyusb speed=3`** — USB 2.0 High Speed.

---

## Intan RHS2116 — типовой сценарий

### 1. Сборка с Intan

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake -DWITH_INTAN_HW=ON
cmake --build build
# прошить ELF
```

### 2. Проверка SPI (UART)

```
ID
READ 255
READRAW 255
```

Ожидаемый chip ID RHS2116: **`0x0020`** (32 decimal). Ответ `ID` также показывает `HI16_OK` / `HI16_BAD` для диагностики SPI.

### 3. Инициализация записи

```
INIT_RECORD 480
CONVERT 0
CONVERT 63
```

Канал **63** — авто-режим (последовательный опрос 16 каналов).

### 4. Проверка по USB

```bash
python3 tools/usb_intan_cmd.py ID
python3 tools/usb_intan_cmd.py CONVERT 0 0
```

### Протокол SPI (кратко)

- Каждая операция READ / WRITE / CONVERT — **три отдельные CS-транзакции** (как в Linux `intan_spi.c`).
- Кадр — **32 бита**; упаковка байт: `(b0<<24)|(b1<<16)|(b2<<8)|b3`.
- `Intan_SPI4_HwInit()` программирует SPI2 напрямую (без `HAL_SPI_TransmitReceive`).
- CS по умолчанию **PE11**, между транзакциями — HIGH.

---

## Тесты скорости (ksps)

### UART — чистый SPI/Intan (без USB overhead)

После `INIT_RECORD [ksps]`:

| Команда | Путь | Описание |
|---------|------|----------|
| `BENCH n [ch]` | Safe 3-slot | Эталон корректности, медленнее |
| `BENCH_FAST n [ch]` | Pipelined | CS на каждый слот |
| `BENCH_DMA n [ch]` | SPI2 DMA + TIM1_CH2 CS | **Максимальная** скорость SPI (~700+ ksps_total) |
| `BENCH_TIMCS n [ch] [target_ksps]` | TIM1 CS | Целевая частота 100–720 ksps (alias: `BENCH_TIM`) |

Параметры:

- **`n`** — число CONVERT (1…2 000 000)
- **`ch`** — канал 0–62 или **63** (авто, 16 каналов)
- Ответ: **`ksps_total`**, **`ksps_per_ch`** (= total/16 для ch=63)

Примеры (UART):

```
INIT_RECORD 480
BENCH 50000 63
BENCH_FAST 50000 63
BENCH_DMA 50000 63
BENCH_TIMCS 50000 63 600
```

Пример ответа:

```
OK BENCH_DMA n=50000 ch=63 ksps_total=714.123 ksps_per_ch=44.633
```

### USB — end-to-end (Intan + bulk)

```bash
python3 tools/usb_stream_bench.py -n 50000 --channel 0
```

Использует тот же **DMA+TIM1 CS** путь, что и `BENCH_DMA`, но с ограничением USB (пакеты 512 B, main-loop scheduling). Ожидаемый порядок: **~500–560 ksps_total** (зависит от хоста и hub).

### USB transport only (без Intan)

Не измеряет ksps Intan, только канал:

```bash
python3 tools/usb_bulk_loopback.py --size 512
python3 tools/usb_intan_cmd.py PING
```

---

## Стимуляция

Последовательность (UART), совместима с логикой `stimulate_channel0.py` из Linux-проекта:

```
INIT_STIM
STIM_SETUP spec neg_ua pos_ua
STIM_ON spec [pol]
STIM_OFF [spec]
```

Паттерны:

```
STIM_PULSE ch pos_ua hold_ms [neg_ua]
STIM_SAW spec steps max_ua period_ms cycles
```

Пример:

```
INIT_STIM
STIM_SETUP 0 0 100
STIM_PULSE 0 50 10
STIM_OFF 0
```

---

## Диагностика и типичные ошибки

| Симптом | Вероятная причина | Что делать |
|---------|-------------------|------------|
| UART полностью молчит | Падение в `Error_Handler` до `MX_USART1` (HSE/LSE/clocks) | Проверить HSE 8 MHz; смотреть SOS на PB6; не включать LSE в `SystemClock_Config` |
| `device 0483:5741 not found` | USB3300 не enumerates | Переподключить USB3300 после ST-Link; проверить PLL3/XTAL; GPIO ULPI не в ANALOG |
| `[M] WARN USB33RDY is not set` | Питание USB33 | Проверить схему питания USB PHY |
| Enumeration обрывается | UART/`snprintf` в USB ISR; TX >512 B | Не логировать из `HAL_PCD_*Callback`; дробить transmit |
| `ERR no intan hw` | Сборка без `-DWITH_INTAN_HW=ON` | Пересобрать с Intan или использовать PING/ECHO |
| `ERR spi` на ID | Intan не запаян / неверные пины / нет питания | PE11 CS, PA9/PB14/PC1, внешнее питание RHS2116 |
| `[R] FAIL LSE` | Нет 32 kHz кварца | `-DBOARD_HAS_LSE=OFF` (не критично для USB) |
| PyUSB: 0 devices (macOS) | Нет libusb | `brew install libusb` |
| `pyusb speed` не 3 | Full Speed fallback | Кабель, USB3300, ULPI init; сравнить с WorkingVER |
| Повтор `BOOT` в логе | Ручной reset платы | Не обязательно software loop |

### Чеклист USB bring-up

1. Прошить ELF через ST-Link
2. Открыть UART 115200 — увидеть `INTAN_UART_READY`
3. **Отключить и подключить USB3300** к Mac/Pi
4. `python3 tools/usb_intan_cmd.py PING` → `OK PONG`
5. Проверить `speed == 3`
6. При Intan: `ID`, затем `usb_stream_bench.py`

---

## Эталон WorkingVER и ulpi-fw

### WorkingVER/STM32H743/

**Эталон рабочего USB на этой же плате:**

- USB **CDC** `0483:5740` (не vendor bulk)
- Тот же порядок clocks / USB3300 / GPIO
- Использовать для **сравнения**, если основной проект не enumerates

### ulpi-fw/

Минимальная автономная прошивка только для проверки ULPI:

- PID **`0483:5742`**
- Сборка:

```bash
cd ulpi-fw
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

- Host: `ulpi-fw/tools/usb_test.py` или `tools/usb_ulpi_test.py`

---

## STM32CubeMX и регенерация

- Источник пинов/periphery: **`WeActSTM32H743.ioc`**
- Пользовательский код — только в блоках **`USER CODE BEGIN/END`**
- После регенерации Cube проверять:
  - `SystemClock_Config` в `main.c` (может расходиться с `.ioc` — **приоритет у main.c + WorkingVER**)
  - GPIO ULPI не переведены в ANALOG
  - Не возвращён `HAL_SPI_Init` путь поверх `Intan_SPI4_HwInit`
- FreeRTOS в текущей конфигурации **не используется**

---

## Справочные материалы

| Ресурс | Путь |
|--------|------|
| Контекст для агентов | [`AGENTS.md`](AGENTS.md) |
| Linux/Python референс | [`msu-neuro-terminal-linux/`](msu-neuro-terminal-linux/) |
| Драйвер SPI Intan (ядро) | `msu-neuro-terminal-linux/driver/intan_spi.c` |
| Datasheet RHS2116 | `msu-neuro-terminal-linux/Intan RHS2116/Intan_RHS2116_datasheet.pdf` |
| Конспект даташита (RU) | `msu-neuro-terminal-linux/Intan RHS2116/RHS2116_datasheet_notes_ru 2.md` |
| STM32Cube FW_H7 | V1.13.0 (см. `.ioc`) |
| STM32CubeMX | 6.17.x |

### Версии и лицензия

Проект основан на STM32Cube HAL (ST license). Пользовательские модули Intan/USB — часть этого репозитория.

---

## Краткая шпаргалка

```bash
# Сборка (USB test, без Intan)
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake && cmake --build build

# USB test
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_bulk_loopback.py --size 512

# С Intan (UART)
INIT_RECORD 480
ID
BENCH_DMA 50000 63

# С Intan (USB speed)
python3 tools/usb_stream_bench.py -n 50000 --channel 0
```
