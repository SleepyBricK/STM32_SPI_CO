# Обзор проекта

WeActSTM32H743 - прошивка для пользовательской платы на `STM32H743VIT6`, которая управляет Intan `RHS2116` по SPI2 и передаёт данные на хост по USB HS Vendor Bulk. USB-интерфейс имеет VID:PID `0483:5741`, OUT endpoint `0x01` для текстовых команд и IN endpoint `0x81` для ответов и бинарных `RHS1`-фреймов.

Проект вырос из порта WeAct STM32H743, но текущий USB-код не наследуется от старого CDC-проекта в `WorkingVER/STM32H743/`. Исторический CDC-проект можно использовать только как reference, не как источник для нового Vendor Bulk транспорта.

## Ключевые характеристики

| Область | Значение |
| --- | --- |
| MCU | STM32H743VIT6, Cortex-M7, LQFP100 |
| HSE | 8 MHz на PH0/PH1 |
| SYSCLK по умолчанию | 480 MHz, VOS0, `BOARD_SYSCLK_480=ON` |
| AHB | 240 MHz при SYSCLK 480 MHz |
| Intan SPI | SPI2, 32-bit frame, SCK около 25 MHz |
| Intan CS | `PA11/SPI2_NSS`, hardware pulsed NSS |
| USB | OTG HS + external USB3300 ULPI, PCD DMA выключен |
| Stream ABI | `RHS1`, 4096 байт, 32-байтный header + 2032 `uint16_t` |
| Frame ring | 32 фрейма в `.dma_buffer` D2 SRAM, ready FIFO 64 |

## Production-режим

Единственный валидированный production-режим:

```text
SPI_STREAM_FW <samples_per_channel> 255 0 40
```

`255` означает RR8: последовательность из восьми `CONVERT` по каналам `0..7`. Последний аргумент `40` задаёт 40 kS/s/ch; если `ksps` пропущен или равен `0`, firmware также выбирает safe default `INTAN_FW_KSPS_DEFAULT=40`.

Для production RR8 firmware принудительно ставит SPI timing `PSCL=8` и `MIDI=4`. Команды `SPI_PSCL` и `NSS_MIDI` во время active/armed Framework stream возвращают `ERR busy`.

## Что валидировано

Валидированный стендовый критерий из текущего контекста проекта:

| Проверка | Ожидание |
| --- | --- |
| `ch_fw_long_suite.py --duration 10 --ksps 40` | 10 s RR8 capture без переполнения |
| `STATS` | `sysclk_mhz=480`, `sck_khz=25000` |
| Production capture | `sample_clip=0`, `usb_ovf=0` |
| USB topology | `lsusb -t` показывает High Speed `480M` |
| Аналоговый sanity check | ch2 с 10 kOhm на GND около 64 uV RMS |

## Ограничения

`SPI_STREAM_FW ... 40` - production path. `SPI_STREAM_FW_MAX`, `SPI_STREAM_REAL*`, RR/range и slot-DMA режимы являются диагностическими или legacy. Они полезны для bench, но не должны подменять production RR8.

Каждый 32-битный RHS2116 word должен быть отдельной CS-транзакцией: `CS low -> 32-bit transfer -> CS high`. Нельзя удерживать CS низким на несколько слов, группировать команды стимуляции или ускорять `PATTERN_RUN` через DMA/grouped path.

High-rate режимы выше production, особенно `ksps >= 55`, не считаются production. В коде оставлены диагностические счётчики и bench tools, но безопасная рабочая точка проекта - RR8 40 kS/s/ch.

Дополнительно: LSE не участвует в раннем старте. `SystemClock_Config()` поднимает HSE; LSE включается позже в `MX_RTC_Init()` только при `BOARD_HAS_LSE=ON`.
