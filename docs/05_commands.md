# Справочник USB-команд

Команды передаются ASCII-строкой на bulk OUT `0x01`. Парсер регистронезависимый, разделители - пробелы/табуляции, большинство чисел читается через `strtoul`; для многих аргументов поддерживаются `0x...`. Ответ приходит на bulk IN `0x81` как короткая ASCII-строка.

Команды, зависящие от Intan, при сборке `WITH_INTAN_HW=OFF` возвращают `ERR no intan hw`.

## Общие команды

| Команда | Класс | Синтаксис | Ответ/эффект |
| --- | --- | --- | --- |
| `PING` | control | `PING` | `PONG` |
| `STOP` | control | `STOP` | Запрашивает останов stream; `OK` после teardown |
| `STATS` | diagnostic | `STATS` | Одна строка key=value counters/clocks/fault/build |
| `SYNTH_STREAM` | diagnostic | `SYNTH_STREAM <samples>` | Генерирует counter `RHS1` frames |
| `NSS_MIDI` | tuning | `NSS_MIDI <0..15>` | Меняет CS high gap; `ERR busy` при FW stream |
| `SPI_PSCL` | tuning | `SPI_PSCL <div>` | Меняет SPI prescaler; `ERR busy` при FW stream |

`STATS` включает `samples`, `frames_out`, `spi_xfer32`, `xfer_per_resp_x1000`, `usb_ovf`, `spi_ovf`, `tx_err`, `sample_clip`, `rx_off`, `fw_dma_err`, `sysclk_mhz`, `spi_khz`, `sck_khz`, `pscl`, `nss_midi`, `tim_p`, `cyc_samp`, `ksps_cyc_x10`, `wall_cyc`, `wall_ksps_x10`, `usb_disconnect`, `fw_late_seq`, `samples_dropped`, `iwdg_reset`, `last_fault`, `build_type`, `git`.

Пример:

```bash
python3 tools/usb_intan_cmd.py STATS --no-reset
```

## RHS2116 register/control

| Команда | Класс | Синтаксис | Диапазоны | Ответ |
| --- | --- | --- | --- | --- |
| `ID` | diagnostic | `ID` | - | `OK ID chip=... raw32=...` |
| `READ` | diagnostic | `READ <reg>` | `reg <= 255` | `OK READ reg=... value=...` |
| `WRITE` | diagnostic | `WRITE <reg> <value> [u] [m]` | `reg <= 255`, `value <= 0xFFFF` | `OK WRITE` |
| `INIT_RECORD` | prep | `INIT_RECORD [adc_ksps]` | `0` выбирает `480` | `OK INIT_RECORD <target>` |
| `INIT_STIM` | prep | `INIT_STIM` | - | `OK INIT_STIM` |
| `CLEAR_ADC` | prep | `CLEAR_ADC` | - | `OK CLEAR_ADC` |
| `CLEAR_COMP` | prep | `CLEAR_COMP` | - | `OK CLEAR_COMP` |
| `CONVERT` | diagnostic | `CONVERT <channel> [flags] [d]` | `channel <= 63` | `OK CONVERT ch=... flags=... value=...` |
| `IMPEDANCE_MEASURE` | diagnostic | `IMPEDANCE_MEASURE <ch> <scale_bits> <freq_hz> <samples_per_period> <periods> <flags>` | См. ниже | `OK IMPEDANCE ...` |

`CONVERT` собирает flags так: второй аргумент попадает в `flags`, третий аргумент, если ненулевой, добавляет bit `0x02`.

`IMPEDANCE_MEASURE` ограничения из кода:

| Параметр | Диапазон |
| --- | --- |
| `channel` | `0..15` |
| `scale_bits` | `0`, `1` или `3` |
| `freq_hz` | `10..10000` |
| `samples_per_period` | `4..128` |
| `periods` | `1..1000` |
| `flags` | Младшие 4 бита |

Подробности измерения импеданса: [intan_impedance_guide.md](../intan_impedance_guide.md).

## Production acquisition

| Команда | Класс | Синтаксис | Назначение |
| --- | --- | --- | --- |
| `SPI_STREAM_FW` | production/diagnostic | `SPI_STREAM_FW <n> <channel> <flags> <ksps>` | Framework acquisition |
| `SPI_STREAM_FW_MAX` | diagnostic | `SPI_STREAM_FW_MAX <n> <channel> <flags>` | Freerun/max throughput |

Production:

```text
SPI_STREAM_FW <samples_per_channel> 255 0 40
```

`channel=255` означает all/RR8, каналы `0..7`. Для single-channel FW stream допустимы `channel=0..7`. Если `channel != 255` и `channel >= 8`, firmware отвечает `ERR fw stream ch>=8 (use 255 for all)`.

`ksps=0` в `SPI_STREAM_FW` выбирает default 40 kS/s/ch. `SPI_STREAM_FW_MAX` передаёт в firmware специальный freerun target и не является production.

Подробнее: [06_acquisition.md](06_acquisition.md).

## Legacy/diagnostic streams

Эти команды используются для bench и отладки. Для real ADC потоков firmware перед стартом делает `INIT_RECORD` на внутреннем target `INTAN_APP_RECORD_ADC_KSPS` и reset HPF через priming `CONVERT`.

| Команда | Синтаксис | Payload | Комментарий |
| --- | --- | --- | --- |
| `SPI_STREAM` | `SPI_STREAM <samples> <channel> <flags>` | counter | SPI/DMA + USB frames |
| `SPI_STREAM_REAL` | `SPI_STREAM_REAL <samples> <channel> <flags>` | ADC | Real single channel; `channel=63` запрещён |
| `SPI_STREAM_REAL_FAST` | `SPI_STREAM_REAL_FAST <samples> <channel> <flags>` | ADC | Polling diagnostic |
| `SPI_STREAM_REAL_SLOT` | `SPI_STREAM_REAL_SLOT <samples> <channel> <flags>` | ADC | TIM slot diagnostic |
| `SPI_STREAM_REAL_LEGACY` | `SPI_STREAM_REAL_LEGACY <samples> <channel> <flags>` | ADC | Old TIM+DMA CS diagnostic |
| `SPI_STREAM_RR8` | `SPI_STREAM_RR8 <samples> <flags>` | counter/tagged metadata | 8-channel RR counter |
| `SPI_STREAM_RR8_REAL` | `SPI_STREAM_RR8_REAL <samples> <flags>` | ADC tagged | RR8 real ADC |
| `SPI_STREAM_RR8_REAL_SLOT` | `SPI_STREAM_RR8_REAL_SLOT <samples> <flags>` | ADC tagged | RR8 TIM slot |
| `SPI_STREAM_RR16_REAL` | `SPI_STREAM_RR16_REAL <samples> <flags>` | ADC tagged | 16-channel RR diagnostic |
| `SPI_STREAM_RANGE_REAL` | `SPI_STREAM_RANGE_REAL <samples> <first> <count> <flags>` | ADC tagged | Range diagnostic |
| `SPI_STREAM_RANGE_REAL_SLOT` | `SPI_STREAM_RANGE_REAL_SLOT <samples> <first> <count> <flags>` | ADC tagged | Range TIM slot |

Range constraints: `first < 16`, `count > 0`, `first + count <= 16`.

## SPI rate/RAM diagnostics

| Команда | Синтаксис | Назначение |
| --- | --- | --- |
| `SPI_RATE` | `SPI_RATE <samples> <channel> <flags>` | Выполнить SPI reads и вернуть rate line |
| `SPI_RATE_FAST` | `SPI_RATE_FAST <samples> <channel> <flags>` | Fast polling rate diagnostic |
| `SPI_RATE_RR8` | `SPI_RATE_RR8 <samples> <flags>` | RR8 rate diagnostic |
| `SPI_TO_RAM` | `SPI_TO_RAM <samples> <channel> <flags>` | Собрать counter payload в RAM без USB pump |
| `SPI_TO_RAM_FAST` | `SPI_TO_RAM_FAST <samples> <channel> <flags>` | Fast polling to RAM |
| `SPI_TO_RAM_RR8` | `SPI_TO_RAM_RR8 <samples> <flags>` | RR8 to RAM diagnostic |

Эти команды не предназначены для production capture. Они полезны, когда нужно сравнить wall-cycle rate, `spi_xfer32`, `xfer_per_resp_x1000`, `sck_khz` и phase state.

## Stim pattern commands

Pattern API хранит массив слотов и выполняет их строго послотово через `Intan_Xfer32Word()`.

| Команда | Синтаксис | Слоты | Ответ |
| --- | --- | ---: | --- |
| `PATTERN_CLEAR` | `PATTERN_CLEAR` | reset | `OK PATTERN_CLEAR` |
| `PATTERN_ADD_RAW` | `PATTERN_ADD_RAW <word32>` | 1 SPI | `OK PATTERN_ADD_RAW` |
| `PATTERN_ADD_WRITE` | `PATTERN_ADD_WRITE <reg> <value> [u] [m]` | 3 SPI | `OK PATTERN_ADD_WRITE` |
| `PATTERN_ADD_READ` | `PATTERN_ADD_READ <reg>` | 3 SPI | `OK PATTERN_ADD_READ` |
| `PATTERN_ADD_CONVERT` | `PATTERN_ADD_CONVERT <channel> <flags>` | 3 SPI | `OK PATTERN_ADD_CONVERT` |
| `PATTERN_ADD_CLEAR_ADC` | `PATTERN_ADD_CLEAR_ADC` | 3 SPI | `OK PATTERN_ADD_CLEAR_ADC` |
| `PATTERN_ADD_CLEAR_COMP` | `PATTERN_ADD_CLEAR_COMP` | 3 SPI | `OK PATTERN_ADD_CLEAR_COMP` |
| `PATTERN_ADD_DELAY_CYC` | `PATTERN_ADD_DELAY_CYC <cycles>` | 1 delay | `OK PATTERN_ADD_DELAY_CYC` |
| `PATTERN_ADD_DELAY_US` | `PATTERN_ADD_DELAY_US <us>` | 1 delay | `OK PATTERN_ADD_DELAY_US` |
| `PATTERN_STATUS` | `PATTERN_STATUS` | - | `OK PATTERN_STATUS loaded=... running=... slots=...` |
| `PATTERN_RUN` | `PATTERN_RUN [repeat]` | execute | `OK PATTERN_RUN` |

`PATTERN_RUN` принимает `repeat=1..10000`; пропущенный аргумент даёт `1`. После выполнения код дополнительно делает `WRITE R42=0 U=1`, чтобы выключить stim output.

Подробности стимуляции и тестов: [intan_stim_pattern_guide.md](../intan_stim_pattern_guide.md) и [intan_pattern_testing_guide.md](../intan_pattern_testing_guide.md).

## Ошибки

Типичные ответы:

| Ответ | Причина |
| --- | --- |
| `ERR unknown` | Команда не распознана |
| `ERR no intan hw` | Сборка без `WITH_INTAN_HW=ON` |
| `ERR spi not ready` | SPI/Intan не готов |
| `ERR busy` | Попытка менять timing во время FW stream |
| `ERR range`, `ERR reg`, `ERR ch` | Аргументы вне диапазона |
| `ERR init_record` | Не удалось подготовить record path |
| `ERR fw stream start` | Не удалось стартовать `SPI_STREAM_FW` |
| `ERR pattern_add`, `ERR pattern_run` | Ошибка pattern subsystem |
