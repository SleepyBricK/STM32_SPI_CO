# Контекст проекта для агентов (WeActSTM32H743)

Читайте этот файл при новом диалоге или потере контекста. Язык ответов пользователю: **русский**.

В Cursor для автоматического напоминания агентам добавлено правило `.cursor/rules/project-context.mdc` (`alwaysApply: true`), которое отсылает сюда.

## Назначение

Прошивка для платы на **STM32H743VIT6** (Cortex-M7, LQFP100). Проект начинался как порт для отладочной платы **WeAct STM32H743**, затем перенесён на пользовательскую плату с HSE 8 MHz. В репозитории — прошивка STM32 + host-скрипты в **`tools/`** для работы с **Intan RHS2116** по SPI и USB HS streaming.

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
| Питание ядра в коде | **480 MHz**: VOS0 (`BOARD_SYSCLK_480=1`, дефолт). **240 MHz**: VSCALE2 (legacy) |

### Такты (см. `SystemClock_Config` в `Core/Src/main.c`)

- HSE **8 MHz** (PH0/PH1). **LSE 32.768 kHz** (PC14/PC15) — **опционально**, только для RTC (`BOARD_HAS_LSE`).
- **В `SystemClock_Config` включается только HSE** (не LSE). LSE не должен блокировать UART при старте.
- **Дефолт (`BOARD_SYSCLK_480=ON`)**: PLL1 HSE / 4 × 480 / 2 → **SYSCLK = 480 MHz** (VOS0); AHB = **240 MHz** (`RCC_HCLK_DIV2`).
- **Legacy (`BOARD_SYSCLK_480=OFF`)**: PLL1 HSE / 4 × 240 / 2 → **SYSCLK = 240 MHz** (VSCALE2); AHB = **120 MHz**.
- PLL2 (отдельно, для SPI2): HSE / 2 × 100 / 2 → **PLL2P = 200 MHz** — **без изменений** в обоих режимах.

> Проверка на плате: `python3 tools/usb_intan_cmd.py STATS --no-reset` → **`sysclk_mhz=480`** (ожидаемый дефолт).

### Выводы периферии (из `WeActSTM32H743.ioc` + код)

- **SPI2**: PA9 SCK, PB14 MISO, PC1 MOSI; ядро SPI1/2/3 от **PLL2P = 200 MHz**, **SCK ≈ 25 MHz**, кадр RHS2116 **32 бита**. Init только при **`INTAN_HW_PRESENT=1`**.
- **USART1**: PB6 TX, PB7 RX — **115200** 8N1.
- **Отладка**: PA13 SWDIO, PA14 SWCLK.

### Intan RHS2116 (не из Cube — задано в коде)

Файлы: `Core/Inc/intan_spi.h`, `Core/Src/intan_spi.c`, `Core/Src/intan_spi4_hw.c`.

- **CS**: **PE11**, активный низкий (программный, SPI NSS не используется).
- Упаковка кадра: `(b0<<24)|(b1<<16)|(b2<<8)|b3`.

#### Инвариант SPI: CS↑ между каждой командой

**Каждый** 32-битный кадр RHS2116 — **отдельная CS-транзакция**: `CS↓ → transfer 32 bit → CS↑`. Между любыми двумя кадрами линия CS **обязана** подниматься (даташит: tCSOFF ≥ ~100 ns; в `intan_xfer32()` — пауза ~500 ns перед следующим CS↓).

| Путь | CS-циклов на одну логическую операцию |
|------|----------------------------------------|
| `Intan_WriteReg` / `READ` / `CONVERT` | **3** (cmd + pipeline + result) |
| `PATTERN_ADD_RAW` | **1** на слот |
| `PATTERN_ADD_WRITE` / `READ` / `CONVERT` | **3** на слот (не один CS на всю тройку) |
| `SPI_STREAM_*_SLOT` (TIM1 + DMA) | **1** на каждое 32-bit слово в burst |
| `IMPEDANCE_MEASURE` | **1** на каждый `WRITE Reg3` и каждый `CONVERT` |

**Запрещено** (ломает ADC pipeline, triggered-регистры и стим на осцилле):

- держать CS низким на несколько 32-bit слов подряд;
- «группировать» WRITE/ON/OFF в один SPI burst без CS↑;
- ускорять `PATTERN_RUN` через TIM/DMA/grouped CS вместо послотового `Intan_Xfer32Word()`.

Эталон реализации: `Intan_Xfer32Word()` → `intan_xfer32()` в `intan_spi.c`. Stim-паттерны: `intan_stim_pattern_guide.md` (`PATTERN_ADD_RAW` для R42/R44 toggle).

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
| `BOARD_SYSCLK_480` | **ON** | **480 MHz** SYSCLK (VOS0), основной режим; SPI kernel без изменений |
| | OFF | Legacy **240 MHz** (VSCALE2, как WorkingVER) |

Сборка с Intan (типичная):

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON
cmake --build build
python3 tools/usb_intan_cmd.py STATS --no-reset   # sysclk_mhz=480
```

Каталог `build480/` — то же самое (скрипт `./tools/build480.sh`), если нужен отдельный tree.

> После смены `BOARD_SYSCLK_480` нужен **reconfigure** (`cmake -S . -B build ...`). Старый cache без флага мог остаться на 240 MHz (`sysclk_mhz=240` в STATS).

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

## Intan RHS2116 — справочные материалы

- PDF: **`Intan RHS2116/Intan_RHS2116_datasheet.pdf`**
- Конспект: **`Intan RHS2116/RHS2116_datasheet_notes_ru 2.md`**
- **Intan STM32 Framework v1.2** (официальный): конспект **`Intan RHS2116/intan_rhd_rhs_stm32_framework_notes.md`**, PDF: https://intantech.com/files/Intan_RHD_RHS_STM32_Framework.pdf — эталон `H7/rhs_acquisition`. Исходники: **`Intan RHS2116/Intan_RHD_RHS_STM32_Framework_v1_2/`**. Карта Intan→наш код: **`Intan RHS2116/intan_framework_port_map.md`**.

### Pipeline unpack (CONVERT stream)

- **Single-channel** (`SPI_STREAM_REAL`): cold `n+2` слота, unpack `RX[2..]`; hot sub-block (продолжение burst) — `rx_offset=0`, unpack `RX[0..]`.
- **RR/range** (CONVERT(63) prime): cold `2 prime + n + 2 tail`, unpack `RX[4..]`; hot — `rx_offset=0`.
- Unpack: **только upper 16 bit** `(w>>16)` — как framework/datasheet, без dual-half эвристик.
- **STATS**: `sample_clip` — попытка старта SPI DMA до завершения предыдущего burst; `rx_off` — последний unpack offset (2 cold / 0 hot / 4 RR cold).
- **Проверка stream (GND)**: `python3 tools/intan_stream_verify.py --no-reset --ch 2`

## Ограничения и заметки

- FreeRTOS в `.ioc` **не включён**.
- Пины Intan править в **`intan_spi.h`**, `.ioc`.
- Cube `.ioc` может расходиться с `main.c` по clocks — **источник правды для boot: `main.c` + WorkingVER**.
- **USB V2**: producer-consumer — `SPI/DMA → frame ring (.dma_buffer) → USB bulk IN`; SPI **не ждёт** USB; при переполнении ring — `usb_overflow_count`, без блокировок в acquisition path.
- **Проверки host**: `python3 tools/usb_intan_cmd.py PING`; `python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5`; длинный: `-n 5000000 --runs 3`. HS: `lsusb -t` → **480M**.
- **Интеграция SPI (порядок)**: (1) длинный USB-only bench; (2) SPI-only ~713 kS/s; (3) `SPI_STREAM` — TIM+DMA + счётчик в RHS1; (4) `SPI_STREAM_REAL` — реальный RESPONSE.
- **STATS / SPI bench**: `cyc_samp` / `ksps_cyc_x10` — **целевой TIM-slot** (DMA CEN→EOT), не wall-clock. **`wall_cyc` / `wall_ksps_x10`** — фактический DWT over full command (setup + SPI + unpack). При **480 MHz** (дефолт): те же **µs**, но `wall_cyc` ≈ **2×** относительно 240 MHz. Ориентиры 240 MHz: **713 kS/s ≈ wall_cyc 336**. `sck_khz=25000` — норма.
- **USB SPI команды**: stream/bench — `SPI_STREAM_FW` (Intan Framework path: TIM6 → **8×CONVERT** (0..7), 0 AUX, HW NSS DMA, unpack `MISO[ch+2]` → USB), `SPI_STREAM_REAL`, `SPI_STREAM_REAL_SLOT`, `SPI_STREAM_REAL_FAST`, `SPI_STREAM_REAL_LEGACY`, `SPI_STREAM_RR8_REAL`, `SPI_STREAM_RR8_REAL_SLOT`, `SPI_STREAM_RR16_REAL`, `SPI_STREAM_RANGE_REAL`, `SPI_STREAM_RANGE_REAL_SLOT`, `SPI_RATE_RR8`, … Host: `python3 tools/usb_spi_rr8_bench.py`, range scan: `python3 tools/usb_intan_scan_range.py --first 0 --count 16`. **`SPI_STREAM_FW n 255 0 [ksps]`** — **8×CONVERT** (0..7); **`ksps≥15`** (RR8, ch=255): **DWT-paced** hot-loop без TIM6 (стабильно **40 kS/s/ch**, clip=0); **`ksps<15`**: TIM6. **`SPI_STREAM_FW_MAX`** — max free-run (~52 kS/s/ch RR8). Plots: `python3 tools/ch_fw_long_suite.py --ksps 40`. `SPI_STREAM_RR8_REAL`, `SPI_STREAM_RR16_REAL` и `SPI_STREAM_RANGE_REAL` используют slot-DMA (`TIM1_CH2` CS на PE11 + `TIM1_UP` TX DMA + `SPI2_RX` DMA): **CS↑ между каждым 32-bit словом**, period **42 SCK cycles**, CS-high **300 ns**, `INIT_RECORD` Reg0 **350 kS/s**; перед стартом — `CONVERT H=1`. `SPI_STREAM_REAL_FAST` — регистровый polling, `SPI_STREAM_REAL_LEGACY` — старый свободно бегущий TIM+DMA CS path.
- **USB Intan (V1 текст, EP OUT/IN как PING)**: `ID`, `READ reg`, `WRITE reg val [u m]`, `INIT_RECORD [ksps]`, `INIT_STIM`, `CLEAR_ADC`, `CLEAR_COMP`, `CONVERT ch [flags]`, `IMPEDANCE_MEASURE ch scale_bits freq_hz samples_per_period periods flags`, `PATTERN_*`. Перед обычными SPI-командами — `usb_stream_reset_all()` (как STOP); `IMPEDANCE_MEASURE` вместо этого возвращает `ERR busy`, если stream активен. Ответы: `OK ID chip=…`, `OK READ reg=…`, `OK IMPEDANCE ... sin_accum=... cos_accum=... actual_freq_millihz=... averages=1 p0_sin=... p0_cos=...`, `ERR …`. `IMPEDANCE_MEASURE` делает Zcheck на STM32: safe state `Reg2/3/42/44/46/48`, optional `phase_safe` для `Reg1`, paced path через полные 3-slot `Intan_WriteReg(3)` + `Intan_Convert(ch)` на sample, 20 ms settle после enable Zcheck, flush pipeline, zero-mean sin/cos accumulators; затем `Reg2=0`, `Reg3=0x0080`, optional restore regs. Для 10 kΩ ch0: `IMPEDANCE_MEASURE 0 1 1000 16 128 3`, `overruns=0`; подробности в `intan_impedance_guide.md`. `PATTERN_RUN` для стимуляции оставлять старым послотовым path через `Intan_Xfer32Word()`; каждый SPI slot — отдельная транзакция `CS↓ word CS↑` с закрытием SPI transfer. Попытки ускорять generic pattern группировкой SPI-слотов (TIM/DMA или fast CS-pulse block) ломали видимый стим-сигнал. Для точных стим-фаз лучше делать специализированный `STIM_PULSE`/`STIM_PATTERN`, а не собирать всё из generic pattern. Host: `tools/usb_intan_cmd.py` — тот же текстовый формат, что UART CLI V1.

## Обновление этого файла

При существенных изменениях (clocks, Intan, новые CMake-флаги, возврат USB) — обновляйте этот файл.
