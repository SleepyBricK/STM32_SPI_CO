# WeActSTM32H743 — STM32H743 + Intan RHS2116

Прошивка для платы на **STM32H743VIT6** с USB HS Vendor Bulk транспортом и опциональным чипом **Intan RHS2116** по SPI2. Основной поток данных идёт в формате `RHS1`: STM32 читает 32-битные SPI-ответы RHS2116, кладёт 16-битные `RESPONSE` в 4096-байтные USB-фреймы и отправляет их host-приложению.

Оперативный контекст для разработки лежит в `AGENTS.md`. Старый рабочий USB CDC reference оставлен в `WorkingVER/STM32H743`, но актуальная реализация находится в корневом `Core`.

## Железо

| Узел | Значение |
|---|---|
| MCU | STM32H743VIT6, Cortex-M7, LQFP100 |
| HSE | 8 MHz, PH0/PH1 |
| LSE | 32.768 kHz, PC14/PC15, опционально |
| USB | USB_OTG_HS + внешняя ULPI PHY USB3300 |
| USB endpoints | Bulk OUT `0x01`, Bulk IN `0x81` |
| Intan SPI2 | PA9 SCK, PB14 MISO, PC1 MOSI |
| Intan CS | PE11, программный GPIO или TIM1_CH2 для DMA stream |
| SPI clock | PLL2P 200 MHz / prescaler 8 = 25 MHz |

## Сборка

Базовая сборка без Intan hardware:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

С реальным RHS2116:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON
cmake --build build
```

Опции сборки:

| Опция | По умолчанию | Назначение |
|---|---:|---|
| `WITH_INTAN_HW` | `OFF` | Включить SPI2/RHS2116 bringup и реальные Intan-команды |
| `BOARD_HAS_LSE` | `OFF` | Использовать LSE 32.768 kHz для RTC |
| `BOARD_SYSCLK_480` | `OFF` | Экспериментальный SYSCLK 480 MHz, обычно достаточно 240 MHz |

ELF: `build/WeActSTM32H743.elf`.

## Прошивка

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

## Runtime

`main.c` инициализирует GPIO, USART1, RTC, SPI2 при `WITH_INTAN_HW=ON`, USB device и stream service. В основном цикле обрабатываются USB-команды, USB stream TX pump и UART CLI.

UART остаётся диагностическим каналом. USB Vendor Bulk является основным транспортом для команд и потока.

## USB-протокол

Host отправляет текстовые команды по Bulk OUT `0x01`. Ответы и stream-фреймы уходят по Bulk IN `0x81`.

Текстовые ответы короткие (`PONG`, `OK`, `ERR ...`, `samples=...`). Во время активного stream-фрейма текстовый ответ может быть занят тем же IN endpoint, поэтому для скоростного потока основной контроль делается командами `STOP` и чтением фреймов.

## Формат RHS1

Каждый stream-фрейм ровно 4096 байт:

```c
typedef struct __attribute__((aligned(32))) {
  uint32_t magic;                // 0x52485331 = 'RHS1'
  uint16_t version;              // 1
  uint16_t flags;                // counter / real ADC / round-robin
  uint32_t frame_seq;
  uint32_t first_sample_counter;
  uint32_t sample_count;         // <= 2032
  uint32_t spi_overflow_count;
  uint32_t usb_overflow_count;
  uint32_t reserved;             // stream metadata
  uint16_t response[2032];
} UsbStreamFrame;
```

`flags`:

| Флаг | Значение |
|---|---:|
| `USB_STREAM_FLAG_COUNTER` | `0x0001` |
| `USB_STREAM_FLAG_REAL_ADC` | `0x0002` |
| `USB_STREAM_FLAG_RR` | `0x0004` |

`reserved` теперь содержит metadata без изменения размера фрейма:

| Биты | Значение |
|---|---|
| `7:0` | первый канал |
| `15:8` | число каналов |
| `23:16` | CONVERT flags |

Для real ADC payload host пересчитывает AC high-gain в микровольты так:

```text
uV = (response - 32768) * 0.195
```

## Команды USB

Базовые:

```text
PING
STOP
STATS
SYNTH_STREAM <samples>
```

Intan control:

```text
ID
READ <reg>
WRITE <reg> <value> <u> <m>
INIT_RECORD [adc_ksps]
INIT_STIM
CLEAR_ADC
CLEAR_COMP
CONVERT <channel> [flags]
```

Streaming / bench:

```text
SPI_STREAM <samples> <channel> <flags>
SPI_STREAM_REAL <samples> <channel> <flags>
SPI_STREAM_REAL_FAST <samples> <channel> <flags>
SPI_STREAM_REAL_SLOT <samples> <channel> <flags>
SPI_STREAM_REAL_LEGACY <samples> <channel> <flags>
SPI_STREAM_RR8 <samples> <flags>
SPI_STREAM_RR8_REAL <samples> <flags>
SPI_STREAM_RR8_REAL_SLOT <samples> <flags>
SPI_STREAM_RR16_REAL <samples> <flags>
SPI_STREAM_RANGE_REAL <samples> <first> <count> <flags>
SPI_STREAM_RANGE_REAL_SLOT <samples> <first> <count> <flags>
SPI_RATE <samples> <channel> <flags>
SPI_RATE_FAST <samples> <channel> <flags>
SPI_RATE_RR8 <samples> <flags>
SPI_TO_RAM <samples> <channel> <flags>
SPI_TO_RAM_FAST <samples> <channel> <flags>
SPI_TO_RAM_RR8 <samples> <flags>
```

`SPI_STREAM_REAL`, `SPI_STREAM_RR8_REAL`, `SPI_STREAM_RR16_REAL` и `SPI_STREAM_RANGE_REAL` перед стартом автоматически переводят RHS2116 в recording mode через `Intan_App_InitRecord(610)` и выполняют одноразовый `CONVERT` с `H=1` для сброса DSP HPF. Одноканальный, 8/16-канальный и range real stream используют slot-DMA path: `TIM1_CH2` формирует CS на PE11, а `TIM1_UP` запускает TX DMA в `SPI2->TXDR`; RX остаётся на `SPI2_RX` DMA. `*_SLOT` явно выбирает тот же path, `SPI_STREAM_REAL_FAST` оставлен для регистрового polling, `SPI_STREAM_REAL_LEGACY` — для старого свободно бегущего TIM+DMA CS path.

`SPI_STREAM_REAL` не принимает `channel=63`: auto-increment `CONVERT(63)` имеет неоднозначный стартовый канал для host metadata. Для многоканального real stream используйте `SPI_STREAM_RR8_REAL`.

`SPI_STREAM_RR8_REAL` пишет в поток каналы `0..7`, `SPI_STREAM_RR16_REAL` — `0..15`, `SPI_STREAM_RANGE_REAL` — `first..first+count-1` round-robin. Канал для `response[i]` восстанавливается как:

```text
channel = first_channel + (i % channel_count)
```

## Статистика

`STATS` возвращает текстовую строку с текущими счётчиками:

```text
samples=... frames_out=... spi_xfer32=... xfer_per_resp_x1000=...
usb_ovf=... spi_ovf=... tx_err=...
sysclk_mhz=... spi_khz=... sck_khz=... pscl=... tim_p=...
cyc_samp=... ksps_cyc_x10=... wall_cyc=... wall_ksps_x10=...
```

`ksps_*_x10` означает `kSamples/s * 10`. Например `6100` = `610.0 kS/s`.

## Host-утилиты

Отправить одну команду:

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_intan_cmd.py "ID" --no-reset
```

Проверить synthetic stream:

```bash
python3 tools/usb_frame_bench.py -n 50000 --runs 5
```

Проверить real Intan stream:

```bash
python3 tools/usb_frame_bench.py -n 50000 --spi-stream-real --channel 0 --no-reset
python3 tools/usb_frame_bench.py -n 50000 --spi-stream-rr8-real --no-reset
```

RR8 bench:

```bash
python3 tools/usb_spi_rr8_bench.py -n 50000 --stream-real
```

## Основные файлы

```text
Core/Src/main.c                  boot, clocks, main loop
Core/Src/spi.c                   SPI2 clock/pins, low-level init call
Core/Src/intan_spi.c             RHS2116 SPI protocol, DMA+TIM CS stream
Core/Src/intan_spi4_hw.c         direct SPI register access
Core/Src/intan_app.c             INIT_RECORD, INIT_STIM, stimulation helpers
Core/Src/intan_stream.c          pack RESPONSE into RHS1 frames
Core/Src/usb_stream_service.c    command dispatcher, stream producer, stats
Core/Src/usb_vendor_bulk.c       Vendor Bulk class, IN/OUT endpoints
Core/Inc/usb_stream_frame.h      RHS1 frame ABI
tools/                           PyUSB host helpers and benchmarks
```

## Важные ограничения

- `WITH_INTAN_HW=OFF` оставляет USB/UART команды доступными, но реальные Intan-команды вернут `ERR no intan hw`.
- USB stream buffers и SPI DMA buffers лежат в `.dma_buffer` в D2 SRAM (`0x30000000`) и отмечены MPU как non-cacheable.
- `response[]` содержит сырые 16-битные значения RHS2116, не микровольты.
- Для real stream сначала используйте `ID`/`INIT_RECORD` при ручной отладке; stream-команды `*_REAL` уже делают `INIT_RECORD(610)` автоматически.
