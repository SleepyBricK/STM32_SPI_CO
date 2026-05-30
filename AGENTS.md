# Контекст проекта для агентов (WeActSTM32H743)

Читайте этот файл при новом диалоге или потере контекста. Язык ответов пользователю: **русский**.

В Cursor для автоматического напоминания агентам добавлено правило `.cursor/rules/project-context.mdc` (`alwaysApply: true`), которое отсылает сюда.

## Назначение

Прошивка для платы на **STM32H743VIT6** (Cortex-M7, LQFP100). Проект начинался как порт для отладочной платы **WeAct STM32H743**, затем перенесён на пользовательскую плату с HSE 8 MHz. В репозитории есть порт логики работы с **Intan RHS2116** по SPI (совместимость с Linux/Python-проектом в каталоге `msu-neuro-terminal-linux/`).

**USB HS Streaming V2** (vendor bulk `0483:5741`, RHS1 4096 B) — реализован с нуля по **`usb_hs_streaming_v2_clean_slate_guide.md`**. Synthetic `SYNTH_STREAM` на host: **~3 M kS/s**, **~6 MB/s**, **errors=0** (запас ~4× относительно цели **713 kS/s** / **1.426 MB/s**). Следующий риск — **совместная работа SPI + USB**, не сам USB-тракт.

Эталон **старого CDC** (не использовать для нового транспорта): **`WorkingVER/STM32H743/`** (`0483:5740`).

## Железо

| Параметр | Значение |
|----------|----------|
| МК | STM32H743VIT6 |
| Корпус | LQFP100 |
| Макрос CMSIS/HAL | `STM32H743xx` |
| Стартап | `startup_stm32h743xx.s` |
| Линкер | `STM32H743XX_FLASH.ld` |
| Питание ядра в коде | `PWR_LDO_SUPPLY`, **`PWR_REGULATOR_VOLTAGE_SCALE2`** (как WorkingVER) |

### Такты (см. `SystemClock_Config` в `Core/Src/main.c`)

- HSE **8 MHz** (PH0/PH1). **LSE 32.768 kHz** (PC14/PC15) — **опционально**, только для RTC (`BOARD_HAS_LSE`).
- **В `SystemClock_Config` включается только HSE** (не LSE). LSE не должен блокировать UART при старте.
- PLL1 (как WorkingVER): HSE / 4 × 240 / 2 → **SYSCLK = 240 MHz**; AHB = **120 MHz** (`RCC_HCLK_DIV2`).
- PLL2 (отдельно, для SPI2): HSE / 2 × 100 / 2 → **PLL2P = 200 MHz**.

> **`BOARD_SYSCLK_480=ON`**: VOS0 / 480 MHz только для bench SPI; раньше VSCALE0 давал проблемы с UART — не использовать как дефолт.

### Выводы периферии (из `WeActSTM32H743.ioc` + код)

- **SPI2**: PA9 SCK, PB14 MISO, PC1 MOSI; ядро SPI1/2/3 от **PLL2P = 200 MHz**, **SCK ≈ 25 MHz**, кадр RHS2116 **32 бита**. Init только при **`INTAN_HW_PRESENT=1`**.
- **USART1**: PB6 TX, PB7 RX — **115200** 8N1.
- **Отладка**: PA13 SWDIO, PA14 SWCLK.

### Intan RHS2116 (не из Cube — задано в коде)

Файлы: `Core/Inc/intan_spi.h`, `Core/Src/intan_spi.c`, `Core/Src/intan_spi4_hw.c`.

- **CS**: **PE11**, активный низкий.
- Протокол: три CS-транзакции на READ/WRITE/CONVERT; упаковка `(b0<<24)|(b1<<16)|(b2<<8)|b3`.

Сборка **без запаянного Intan** (по умолчанию): **`INTAN_HW_PRESENT=0`** — пропуск `MX_SPI2_Init` и bringup; приём команд UART по-прежнему работает без вывода.

### UART CLI

- **На линии TX USART1 сообщений нет**: `UART_DebugMark` / `UART_EarlyPrint` и ответы CLI (`uart_tx_str`) — заглушки; нет строк старта, `HELP`, `OK`/`ERR`, эхо ввода.
- **RX**: прерывание и разбор строк в `Intan_UART_CLI_Process` сохранены (команды исполняются «втихую»).
- При **`Error_Handler` / HardFault**: только **мигание SOS на PB6** (`UART_SosBlinkPB6`), без UART-текста.
- Файлы: `Core/Src/intan_uart_cli.c`, `Core/Src/intan_app.c`, `Core/Src/usart.c`.

## Сборка

- **CMake**, корень: `CMakeLists.txt`. Тулчейн: **`cmake/gcc-arm-none-eabi.cmake`** (обязательно на Mac, иначе AppleClang).

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

### Опции CMake

| Опция | По умолчанию | Назначение |
|-------|--------------|------------|
| `WITH_INTAN_HW` | **OFF** | Intan RHS2116 на SPI2 (`INTAN_HW_PRESENT=1`) |
| `BOARD_HAS_LSE` | **OFF** | 32.768 kHz на PC14/PC15, RTC через LSE |
| `BOARD_SYSCLK_480` | **OFF** | Эксперимент: **480 MHz** SYSCLK (VOS0) для SPI bench; SPI kernel без изменений |

Пример **480 MHz bench** (отдельный каталог `build480/`):

```bash
./tools/build480.sh
# или:
cmake -S . -B build480 -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DBOARD_SYSCLK_480=ON
cmake --build build480
STM32_Programmer_CLI ... -w build480/WeActSTM32H743.elf ...
python3 tools/usb_intan_cmd.py STATS --no-reset   # sysclk_mhz=480
```

> Важно: `-DBOARD_SYSCLK_480=ON` нужен **при cmake configure**. Пересборка в старом `build/` без этого флага остаётся на **240 MHz** (`sysclk_mhz=240` в STATS).

- ELF: `build/WeActSTM32H743.elf`
- Прошивка: `STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst -w build/WeActSTM32H743.elf -v -rst`

## Версии инструментов (ориентиры)

- STM32CubeMX 6.17.x, пакет **STM32Cube FW_H7 V1.13.0** (см. `.ioc`).

## Структура исходников (важное)

| Путь | Содержание |
|------|------------|
| `Core/Src/main.c` | Boot, clocks, main loop (USB stream + UART + Intan) |
| `Core/Src/gpio.c` | Clock enable A/B/C/H; PE analog (Intan) |
| `Core/Src/rtc.c` | LSE только при `BOARD_HAS_LSE=1` |
| `Core/Src/intan_spi.c`, `intan_spi4_hw.c` | Intan SPI (если `INTAN_HW_PRESENT=1`) |
| `Core/Src/intan_stream.c` | Producer в frame ring (заглушка → SPI RESPONSE) |
| `Core/Src/usb_stream_*.c`, `usb_vendor_bulk.c`, `usb_device.c` | RHS1 ring, vendor bulk, ULPI |
| `Core/Src/intan_uart_cli.c` | UART CLI |
| `Core/Src/intan_app.c` | INIT_RECORD, бенчи, стим-паттерны |
| `tools/usb_frame_bench.py` | Валидация RHS1; `FRAME_HDR = "<IHHIIIIII"` (32 B, 9 полей) |
| `WorkingVER/STM32H743/` | Эталон CDC (исторический) |
| `WeActSTM32H743.ioc` | Cube; не ломать блоки `USER CODE` |

## Справочный проект Linux

Каталог **`msu-neuro-terminal-linux/`**: `driver/intan_spi.c`, Python, DTS.

- PDF: **`msu-neuro-terminal-linux/Intan RHS2116/Intan_RHS2116_datasheet.pdf`**
- Конспект: **`msu-neuro-terminal-linux/Intan RHS2116/RHS2116_datasheet_notes_ru 2.md`**

## Ограничения и заметки

- FreeRTOS в `.ioc` **не включён**.
- Пины Intan править в **`intan_spi.h`**, `.ioc`.
- Cube `.ioc` может расходиться с `main.c` по clocks — **источник правды для boot: `main.c` + WorkingVER**.
- **USB V2**: producer-consumer — `SPI/DMA → frame ring (.dma_buffer) → USB bulk IN`; SPI **не ждёт** USB; при переполнении ring — `usb_overflow_count`, без блокировок в acquisition path.
- **Проверки host**: `python3 tools/usb_intan_cmd.py PING`; `python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5`; длинный: `-n 5000000 --runs 3`. HS: `lsusb -t` → **480M**.
- **Интеграция SPI (порядок)**: (1) длинный USB-only bench; (2) SPI-only ~713 kS/s; (3) `SPI_STREAM` — TIM+DMA + счётчик в RHS1; (4) `SPI_STREAM_REAL` — реальный RESPONSE.
- **STATS / SPI bench**: `cyc_samp` / `ksps_cyc_x10` — **целевой TIM-slot** (DMA CEN→EOT), не wall-clock. **`wall_cyc` / `wall_ksps_x10`** — фактический DWT over full command (setup + SPI + unpack). При 240 MHz: **713 kS/s ≈ wall_cyc 336**, **562 kS/s ≈ 427**, **495 kS/s ≈ 485**. `sck_khz=25000` — норма.
- **USB SPI команды**: stream/bench — `SPI_STREAM_REAL`, `SPI_STREAM_REAL_SLOT`, `SPI_STREAM_REAL_FAST`, `SPI_STREAM_REAL_LEGACY`, `SPI_STREAM_RR8_REAL`, `SPI_STREAM_RR8_REAL_SLOT`, `SPI_RATE_RR8`, … Host: `python3 tools/usb_spi_rr8_bench.py`. `SPI_STREAM_REAL` и `SPI_STREAM_RR8_REAL` сейчас используют slot-DMA (`TIM1_CH2` CS на PE11 + `TIM1_UP` TX DMA + `SPI2_RX` DMA), period **44 SCK cycles**, CS-high **300 ns**, и перед стартом делают одноразовый `CONVERT H=1` для сброса DSP HPF. `SPI_STREAM_REAL_FAST` — регистровый polling, `SPI_STREAM_REAL_LEGACY` — старый свободно бегущий TIM+DMA CS path.
- **USB Intan (V1 текст, EP OUT/IN как PING)**: `ID`, `READ reg`, `WRITE reg val [u m]`, `INIT_RECORD [ksps]`, `INIT_STIM`, `CLEAR_ADC`, `CLEAR_COMP`, `CONVERT ch [flags]`. Перед ними — `usb_stream_reset_all()` (как STOP). Ответы: `OK ID chip=…`, `OK READ reg=…`, `ERR …`. Stimulator (`msu-neuro-terminal-linux`) — тот же формат, что UART CLI V1.

## Обновление этого файла

При существенных изменениях (clocks, Intan, новые CMake-флаги, возврат USB) — обновляйте этот файл.
