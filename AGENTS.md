# Контекст проекта для агентов (WeActSTM32H743)

Читайте этот файл при новом диалоге или потере контекста. Язык ответов пользователю: **русский**.

В Cursor для автоматического напоминания агентам добавлено правило `.cursor/rules/project-context.mdc` (`alwaysApply: true`), которое отсылает сюда.

## Назначение

Прошивка для платы на **STM32H743VIT6** (Cortex-M7, LQFP100). Проект начинался как порт для отладочной платы **WeAct STM32H743**, затем перенесён на пользовательскую плату с HSE 8 MHz и USB3300 ULPI. В репозитории есть порт логики работы с **Intan RHS2116** по SPI (совместимость с Linux/Python-проектом в каталоге `msu-neuro-terminal-linux/`).

Эталон **рабочего USB на этой плате**: каталог **`WorkingVER/STM32H743/`** (CDC `0483:5740`, без Intan). Основной проект — vendor bulk `0483:5741` + Intan; **порядок clocks/USB3300/GPIO** должен совпадать с WorkingVER.

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
- **В `SystemClock_Config` включается только HSE** (не LSE). LSE не должен блокировать UART/USB при старте.
- PLL1 (как WorkingVER): HSE / 4 × 240 / 2 → **SYSCLK = 240 MHz**; AHB = **120 MHz** (`RCC_HCLK_DIV2`).
- PLL2 (отдельно, для SPI2): HSE / 2 × 100 / 2 → **PLL2P = 200 MHz**.
- USB kernel clock: **PLL3** → **48 MHz** (`RCC_USBCLKSOURCE_PLL3` в `usb3300_ulpi_hw.c`, HSE / 4 × 96 / 4).

> Старый вариант VSCALE0 / 480 MHz + LSE в `HAL_RCC_OscConfig` при boot давал «тишину» на UART и нестабильный USB — **не возвращать без причины**.

### Выводы периферии (из `WeActSTM32H743.ioc` + код)

- **SPI2**: PA9 SCK, PB14 MISO, PC1 MOSI; ядро SPI1/2/3 от **PLL2P = 200 MHz**, **SCK ≈ 25 MHz**, кадр RHS2116 **32 бита**. Init только при **`INTAN_HW_PRESENT=1`**.
- **USART1**: PB6 TX, PB7 RX — **115200** 8N1.
- **USB3300 / USB OTG HS ULPI**: PC0 STP, PC2_C DIR, PC3_C NXT, PA3 D0, PA5 CLK, PB0 D1, PB1 D2, PB10–PB13 D3–D6, PB5 D7. Init: `Core/Src/usb3300_ulpi_hw.c` — **10 ms XTAL**, **PLL3 48 MHz**, `DisableUSBReg` + `USB33RDY`, GPIO ULPI, analog switch PC2/PC3.
- **USB 2.0 High Speed device**: … **`Transmit()`** copy для text, **`TransmitZc()`** zero-copy для STREAM; очередь **3** слота; **`TxIdle()`**; PCD **DMA** + cache clean; EP1 TX FIFO **0x180**.
- **Отладка**: PA13 SWDIO, PA14 SWCLK. ST-Link **≠** USB3300: для USB-тестов кабель на **USB3300 → хост**.

### Intan RHS2116 (не из Cube — задано в коде)

Файлы: `Core/Inc/intan_spi.h`, `Core/Src/intan_spi.c`, `Core/Src/intan_spi4_hw.c`.

- **CS**: **PE11**, активный низкий.
- Протокол: три CS-транзакции на READ/WRITE/CONVERT; упаковка `(b0<<24)|(b1<<16)|(b2<<8)|b3`.
- **`BENCH_DMA` / `STREAM`**: SPI2 DMA + TIM1_CH2, ping-pong **2×8190** samples (`.dma_buffer`), `TransmitZc` + **TX queue 3**, USB PCD **DMA** + D-Cache clean, EP1 FIFO **0x180**.

Сборка **без запаянного Intan** (по умолчанию): **`INTAN_HW_PRESENT=0`** — пропуск `MX_SPI2_Init` и bringup; USB `PING`/`ECHO`/`HELP`, UART `PING`/`HELP`; команды Intan → `ERR no intan hw`.

### UART CLI

- После сброса (типовой лог): `EARLY` → `CLK` → `BOOT` → USB `[ULPI]`/`[USB]` → `[R] BOARD_HAS_LSE=0 …` (если LSE off) → `INTAN_UART_READY`.
- Метки: **`[M]`** main, **`[U]`** USART, **`[R]`** RTC, **`[S]`** SPI2, **`[I]`** Intan, **`[C]`** CLI, **`[ULPI]`** / **`[USB]`** USB3300/stack.
- **Ранний UART** до PLL: `UART_EarlyMinInit` / `UART_EarlyPrint` в `usart.c` (`EARLY`, `CLK`). **`Error_Handler`**: SOS на PB6 + `!ERR_HANDLER`.
- **Не логировать из USB ISR** (`HAL_PCD_*Callback`) — блокирующий UART ломает EP0/enumeration.
- Файлы: `Core/Src/intan_uart_cli.c`, `Core/Src/intan_app.c`.

## USB (vendor bulk)

- Init: `USB_DEVICE_Init()` → `DevDisconnect` / 50 ms / `DevConnect`; после полного init — **`USB_DEVICE_FinalizeAttach()`** (late reconnect).
- Main loop: `Intan_USB_Bulk_Process()` + **`USB_DEVICE_PollEvents()`** (счётчики reset/connect, смена `dev_state` без UART в IRQ).
- Команды обрабатываются в **main loop**, не в USB IRQ. OUT → очередь → `dispatch_usb_command` → ответ Bulk IN.
- Текстовые USB-команды (при `INTAN_HW_PRESENT=1`): `PING`, `ECHO`, `ID`, `READ`, `READRAW`, `WRITE r hex [u m]`, `INIT_RECORD [ksps]`, `INIT_STIM`, `CLEAR_ADC`, `CLEAR_COMP`, `CONVERT`, **`BENCH` / `BENCH_FAST` / `BENCH_DMA` / `BENCH_TIMCS n [ch] [target_ksps]`** (замер ksps по SPI, текстовый ответ), `STREAM n [ch] [flags]`. `STREAM` → только бинарный IN (16-bit LE).
- Host: `tools/usb_intan_cmd.py`, `tools/usb_spi_bench.py` (SPI ksps без bulk samples), `tools/usb_stream_bench.py`, `tools/usb_bulk_loopback.py` (echo через `ECHO`). Orange Pi Stimulator: `Stimulator_2.0_orangepizero2w/services/server/intan_usb_transport.py`.

### Проверка на Mac / Linux

```bash
python3 tools/usb_intan_cmd.py PING          # OK PONG
python3 tools/usb_intan_cmd.py ECHO hello
python3 -c "import usb.core; d=usb.core.find(idVendor=0x0483,idProduct=0x5741); print(d.speed)"  # 3 = HS
```

После прошивки через ST-Link: **переподключить кабель USB3300** (или дождаться late reconnect). В UART: `[USB] host reset`, затем `dev_state=3` (CONFIGURED).

`system_profiler SPUSBDataType` на новых macOS часто **пустой** для vendor-устройств — ориентир: **PyUSB + PING**.

### Типичные ошибки (не повторять)

| Симптом | Причина |
|---------|---------|
| UART молчит | LSE/HSE в `SystemClock_Config` до `MX_USART1`, падение в `Error_Handler` без early UART |
| `device 0483:5741 not found`, `rst=0` | Нет PLL3/XTAL ULPI; нет reconnect после ST-Link; GPIO ULPI в ANALOG |
| Enumeration обрыв | UART/`snprintf` в USB ISR; слишком большой `USBD_VENDOR_BULK_Transmit` |
| `[R] FAIL LSE` | Нет кварца на PC14/PC15 — **не критично** для USB; использовать `BOARD_HAS_LSE=OFF` |

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

Пример с Intan и LSE:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DBOARD_HAS_LSE=ON
cmake --build build
```

- ELF: `build/WeActSTM32H743.elf`
- Прошивка: `STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst -w build/WeActSTM32H743.elf -v -rst`

### Автономная ULPI-прошивка (`ulpi-fw/`)

Отдельный проект, VID:PID **`0483:5742`**. Host: `ulpi-fw/tools/usb_test.py`.

## Версии инструментов (ориентиры)

- STM32CubeMX 6.17.x, пакет **STM32Cube FW_H7 V1.13.0** (см. `.ioc`).

## Структура исходников (важное)

| Путь | Содержание |
|------|------------|
| `Core/Src/main.c` | Boot, clocks, порядок init, `USB_DEVICE_FinalizeAttach` |
| `Core/Src/gpio.c` | Clock enable A/B/C/H; PE analog (Intan); **ULPI не в ANALOG** |
| `Core/Src/rtc.c` | LSE только при `BOARD_HAS_LSE=1` |
| `Core/Src/usb3300_ulpi_hw.c` | USB3300: XTAL, PLL3, power, ULPI GPIO |
| `Core/Src/usb_device.c` | USB stack, late reconnect, `PollEvents` |
| `Core/Src/usbd_conf.c` | PCD/ULPI; FIFO TX0=0x40, TX1=0x100; **без UART в callbacks** |
| `Core/Src/usbd_vendor_bulk.c` | Bulk class, TX ≤512 B |
| `Core/Src/intan_usb_bulk.c` | USB-команды Intan / PING / STREAM |
| `Core/Src/intan_spi.c`, `intan_spi4_hw.c` | Intan SPI (если `INTAN_HW_PRESENT=1`) |
| `WorkingVER/STM32H743/` | **Эталон USB+UART** (CDC 5740) для сравнения |
| `WeActSTM32H743.ioc` | Cube; не ломать блоки `USER CODE` |

## Справочный проект Linux

Каталог **`msu-neuro-terminal-linux/`**: `driver/intan_spi.c`, Python, DTS.

- PDF: **`msu-neuro-terminal-linux/Intan RHS2116/Intan_RHS2116_datasheet.pdf`**
- Конспект: **`msu-neuro-terminal-linux/Intan RHS2116/RHS2116_datasheet_notes_ru 2.md`**

## Ограничения и заметки

- FreeRTOS в `.ioc` **не включён**.
- Пины Intan/USB править в **`intan_spi.h`**, `usb3300_ulpi_hw.c`, `.ioc`.
- Cube `.ioc` может расходиться с `main.c` по clocks — **источник правды для boot/USB: `main.c` + WorkingVER**.

## Обновление этого файла

При существенных изменениях (clocks, USB, Intan, новые CMake-флаги) — обновляйте этот файл.
