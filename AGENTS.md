# WeActSTM32H743: контекст проекта

Читайте этот файл при новом диалоге и перед изменениями архитектуры. Отвечайте пользователю по-русски.

Глобальное правило `~/.cursor/rules/codex-delegation.mdc` задаёт делегирование сложных задач в Codex для всех проектов.

## Codex (этот репозиторий)

Рабочая директория: `/Users/warforterritory/STM32Cube/WeActSTM32H743`.

При вызове Codex передавайте этот файл как контекст. Песочница: `read-only` для анализа, `workspace-write` для изменений кода.

## Назначение

Прошивка пользовательской платы на **STM32H743VIT6** для работы с **Intan RHS2116** по SPI2 и USB HS Vendor Bulk. Проект вырос из порта WeAct STM32H743; текущая плата использует HSE 8 MHz.

Основной транспорт -- USB HS Vendor Bulk `0483:5741`, endpoint OUT `0x01`, IN `0x81`. Поток передаётся фреймами `RHS1` по 4096 байт. Старый CDC-проект в `WorkingVER/STM32H743/` -- только исторический reference, не источник для нового USB-кода.

## Плата и тактирование

| Параметр | Значение |
| --- | --- |
| MCU | STM32H743VIT6, Cortex-M7, LQFP100 |
| CMSIS/HAL macro | `STM32H743xx` |
| HSE | 8 MHz, PH0/PH1 |
| LSE | 32.768 kHz, PC14/PC15, только при `BOARD_HAS_LSE=ON` |
| SYSCLK default | 480 MHz, VOS0, `BOARD_SYSCLK_480=ON` |
| SYSCLK legacy | 240 MHz, VSCALE2, `BOARD_SYSCLK_480=OFF` |
| AHB | 240 MHz при 480 MHz SYSCLK; 120 MHz при 240 MHz SYSCLK |
| SPI2 kernel | PLL2P = 200 MHz в обоих режимах |

`SystemClock_Config()` включает только HSE. LSE поднимается позже в `MX_RTC_Init()` и не должен задерживать старт.

## Intan и SPI2

- SPI2: PA9 SCK, PB14 MISO, PC1 MOSI; кадр RHS2116 -- 32 бита; SCK примерно 25 MHz (`PLL2P / 8`).
- CS целевой платы: **PA11 / SPI2_NSS**, активный низкий, hardware pulsed NSS (`INTAN_CS_HW_NSS=ON`).
- PE11 -- только legacy GPIO/TIM1 path при `INTAN_CS_HW_NSS=OFF`.
- `MX_SPI2_Init()` и bringup выполняются только при `INTAN_HW_PRESENT=1`.

### Обязательный инвариант CS

Каждый 32-битный кадр -- отдельная транзакция: `CS low -> 32-bit transfer -> CS high`. На целевой плате промежуток обеспечивает SPI2 NSSP+MIDI; legacy path обязан поднимать GPIO CS между словами.

- `Intan_WriteReg`, `READ` и `CONVERT` используют по три CS-транзакции.
- `PATTERN_ADD_RAW` -- одна транзакция на слот; `PATTERN_ADD_WRITE/READ/CONVERT` -- три.
- Нельзя удерживать CS низким на несколько 32-битных слов, группировать stim-команды или ускорять `PATTERN_RUN` DMA/grouped path-ом.
- Эталон безопасного послотового пути: `Intan_Xfer32Word()` в `Core/Src/intan_spi.c`.

## Production acquisition

Единственный валидированный production-режим: `SPI_STREAM_FW n 255 0 40`.

- `255` означает RR8: каналы 0..7, восемь `CONVERT` на последовательность.
- `INTAN_FW_KSPS_DEFAULT` = 40 kS/s/ch. Пропущенный или нулевой `ksps` также выбирает 40.
- RR8 с `ksps >= 15` использует DWT phase-paced hot loop, а не TIM6.
- Валидировано: 10 s, `sample_clip=0`, `usb_ovf=0`; ch2 с 10 kOhm на GND около 64 uV RMS.
- Не использовать для production: `ksps >= 55`, `SPI_STREAM_FW_MAX` и solo stream перед RR8.

Legacy `SPI_STREAM_REAL*`, RR/range и slot-DMA команды остаются диагностическими. Перед ними требуется `INIT_RECORD` и priming `CONVERT H=1`; не подменяйте ими RR8 production-path.

## USB V2

Путь данных: `SPI/DMA -> IntanStream -> ring в .dma_buffer -> USB bulk IN`.

- `UsbStreamFrame` строго 4096 байт: 32-байтный заголовок и 2032 `uint16_t` response.
- Ring: 32 фрейма, ready FIFO глубиной 64; буферы находятся в D2 SRAM и MPU-помечены non-cacheable.
- USB PCD работает без peripheral DMA (`dma_enable=DISABLE`).
- Один endpoint IN разделён текстовыми ответами и frame-потоком. При `STOP` или новой команде активный frame transfer прерывается на endpoint до сброса ring, чтобы не переиспользовать его буфер.
- Producer не ждёт USB. При нехватке фреймов инкрементируется `usb_overflow_count`.

RHS1 metadata: `reserved[7:0]` -- first channel, `[15:8]` -- channel count, `[23:16]` -- CONVERT flags, `[26:24]` -- bits per channel tag.

## UART

USART1: PB6 TX, PB7 RX, 115200 8N1. Передача обычных сообщений и CLI-ответов отключена; RX parser сохранён. При Error_Handler/HardFault на PB6 выводится SOS миганием, без UART-текста.

## Сборка

Используйте CMake с `cmake/gcc-arm-none-eabi.cmake`:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build
```

| Опция | Default | Назначение |
| --- | --- | --- |
| `WITH_INTAN_HW` | `OFF` | Intan SPI2 bringup |
| `INTAN_CS_HW_NSS` | `ON` | PA11/SPI2_NSS; OFF включает legacy PE11 |
| `BOARD_HAS_LSE` | `OFF` | RTC от LSE |
| `BOARD_SYSCLK_480` | `ON` | 480 MHz VOS0; OFF -- 240 MHz legacy |

`build/WeActSTM32H743.elf` прошивается через `STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst -w build/WeActSTM32H743.elf -v -rst`.

## Ключевые файлы

| Путь | Роль |
| --- | --- |
| `Core/Src/main.c` | Boot, MPU, clocks, main loop |
| `Core/Src/spi.c` | SPI2 и PA11 hardware NSS |
| `Core/Src/intan_spi.c` | Intan protocol, SPI/DMA paths |
| `Core/Src/intan_fw_acq.c` | RR8 production acquisition |
| `Core/Src/intan_stream.c` | Сборка ADC samples в RHS1 |
| `Core/Src/usb_stream_service.c` | Commands, producer, TX pump, stats |
| `Core/Src/usb_vendor_bulk.c` | Vendor bulk class и endpoint state |
| `Core/Inc/usb_stream_frame.h` | RHS1 ABI |
| `tools/` | Host commands, validation, capture scripts |

## Проверка

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
```

На устройстве ожидаются `sysclk_mhz=480`, `sck_khz=25000`, `sample_clip=0` и `usb_ovf=0` для production capture. Для HS проверьте `lsusb -t`: 480M.

## Правила изменений

- Не трогайте блоки STM32Cube `USER CODE` без необходимости.
- Пины и CS-режим меняйте согласованно в `intan_spi.h`, `spi.c`, `.ioc` и CMake.
- Не меняйте ABI RHS1 без синхронного обновления host tools.
- При изменениях clocks, SPI, USB, CMake или acquisition обновляйте этот файл и `README.md`.
