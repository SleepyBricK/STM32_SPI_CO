# WeActSTM32H743 — прошивка для STM32H743 + Intan RHS2116

Прошивка для пользовательской платы на **STM32H743VIT6** (Cortex-M7, LQFP100). Изначально — порт отладочной платы **WeAct STM32H743**; сейчас адаптирована под плату с **HSE 8 MHz** и опциональным чипом **Intan RHS2116** по SPI2.

**USB-код удалён из основного проекта** — планируется полная переписка. Эталон рабочего USB на этой плате: **`WorkingVER/STM32H743/`** (CDC `0483:5740`).

| Канал | Назначение |
|-------|------------|
| **USART1** (115200, PB6/PB7) | Интерактивный CLI: инициализация Intan, стимуляция, бенчмарки SPI |
| **SWD** (PA13/PA14) | Прошивка и отладка через ST-Link |

Логика протокола Intan согласована с референсным проектом [`msu-neuro-terminal-linux/`](msu-neuro-terminal-linux/).

> **Для AI-агентов в Cursor:** краткий оперативный контекст — в [`AGENTS.md`](AGENTS.md).

---

## Железо

| Параметр | Значение |
|----------|----------|
| МК | STM32H743VIT6, LQFP100 |
| HSE | 8 MHz (PH0/PH1) |
| LSE | 32.768 kHz (PC14/PC15), опционально (`BOARD_HAS_LSE`) |
| Intan CS | PE11 |
| SPI2 | PA9 SCK, PB14 MISO, PC1 MOSI, ~25 MHz, 32-bit кадры |

## Сборка

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake
cmake --build build
```

С Intan и LSE:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DBOARD_HAS_LSE=ON
cmake --build build
```

| Опция | По умолчанию | Назначение |
|-------|--------------|------------|
| `WITH_INTAN_HW` | OFF | Intan RHS2116 на SPI2 |
| `BOARD_HAS_LSE` | OFF | RTC через LSE |

ELF: `build/WeActSTM32H743.elf`

## Прошивка

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

## Первый запуск

1. Прошить ELF через ST-Link
2. Открыть UART **115200** на PB6 (TX)
3. В логе: `EARLY` → `CLK` → `BOOT` → `INTAN_UART_READY`
4. Команды: `HELP`, `PING`

## Структура (основное)

```
Core/Src/
  main.c           — boot, clocks, main loop
  intan_spi.c      — протокол RHS2116
  intan_spi4_hw.c  — низкоуровневый SPI2
  intan_uart_cli.c — UART CLI
  intan_app.c      — INIT_RECORD, бенчи, стим
WorkingVER/STM32H743/  — эталон USB (для будущей переписки)
```

## Эталон USB

Каталог **`WorkingVER/STM32H743/`** — рабочий USB CDC на этой же плате. Использовать как reference при добавлении нового USB в основной проект.

## STM32CubeMX

- Источник пинов: **`WeActSTM32H743.ioc`**
- Пользовательский код — только в блоках **`USER CODE BEGIN/END`**
- После регенерации Cube проверять `SystemClock_Config` в `main.c` (приоритет у `main.c` + WorkingVER)
