# Acquisition

Production acquisition основан на `IntanFw` path: последовательность из восьми `CONVERT`, SPI2 DMA, распаковка pipeline responses и упаковка в `RHS1` stream. Он отличается от legacy `SPI_STREAM_REAL*` тем, что повторяет Framework-style acquisition и валидирован для RR8 40 kS/s/ch.

## Production-команда

```text
SPI_STREAM_FW <samples_per_channel> 255 0 40
```

| Аргумент | Значение для production |
| --- | --- |
| `samples_per_channel` | Сколько samples получить на каждый канал |
| `channel` | `255`, all/RR8 |
| `flags` | `0` |
| `ksps` | `40` kS/s/ch; `0` или пропуск тоже выбирает 40 |

Firmware принимает также single-channel `SPI_STREAM_FW n <0..7> flags ksps`, но production - именно `channel=255`.

## RR8 sequence

`INTAN_FW_CONVERT_SLOTS=8`, `INTAN_FW_AUX_SLOTS=0`. Каждая sequence содержит `CONVERT 0`, `CONVERT 1`, ..., `CONVERT 7`. Ответы RHS2116 имеют pipeline latency `+2`, поэтому распаковка берёт `MISO[channel + 2]`, а последние два канала wrap в `MISO[0]` и `MISO[1]`.

Для `channel=255` в `RHS1` payload пишется восемь `uint16_t` на sequence без channel tags. Хост восстанавливает канал так:

```text
channel = (first_sample_counter + sample_index_in_frame) % 8
```

## DWT phase-paced hot loop

Для all-channel mode при `ksps >= INTAN_FW_KSPS_DWT_PACE_MIN` (`15`) включается DWT-paced path. Production `40` kS/s/ch использует его, а не TIM6 tick pacing.

В DWT-paced режиме:

| Механизм | Поведение |
| --- | --- |
| `s_seq_interval_cyc` | `SystemCoreClock / (ksps * 1000)` |
| `fw_dwt_pace_before_start()` | Ждёт фазу следующей sequence, параллельно flush USB pending |
| Late phase | Если sequence опоздала больше чем на один interval, `fw_late_seq` увеличивается и фаза resync |
| Hot loop в `main()` | Часто вызывает OUT parser, TX pump и `IntanFw_Process()` |

## PSCL/MIDI

При production RR8 (`channel=255`, `ksps=40`) `IntanFw_StreamStart()` принудительно применяет:

| Параметр | Значение |
| --- | ---: |
| SPI prescaler div | `8` |
| SCK | `200 MHz / 8 = 25 MHz` |
| NSS MIDI | `4` SCK cycles |

Команды `SPI_PSCL` и `NSS_MIDI` возвращают `ERR busy`, пока Framework stream active или armed. Это защищает acquisition от изменения timing между стартом команды и фактическим запуском.

## STOP behavior

`STOP` не abort'ит SPI DMA посередине 8-word sequence. Порядок:

1. USB OUT parser ставит stop request.
2. `IntanFw_RequestStop()` сообщает FW path, что после текущей sequence надо остановиться.
3. `IntanFw_Process()` ждёт EOT.
4. Pending samples flush'ятся в `IntanStream`.
5. `UsbStreamService_ProcessStopRequest()` сбрасывает stream/ring и abort'ит активный IN frame.
6. Хост получает `OK`.

Если USB disconnect/reset случается во время stream, teardown идёт по тому же принципу, но watchdog не refresh'ится во время disconnect teardown.

## DMA deadline и ошибки

Каждая 8-word sequence на 25 MHz занимает порядка десятков микросекунд. Production recovery deadline установлен как 1 ms DWT cycles (`SystemCoreClock / 1000`). Если EOT не пришёл до deadline:

| Счётчик/флаг | Значение |
| --- | --- |
| `fw_dma_err` | Увеличивается при unrecovered FW DMA deadline |
| `IntanFw_HasFatalError()` | Становится true после fatal DMA failure |
| IWDG refresh | Прекращается при fatal error |

`Intan_FwSpiDmaHasError()` оставлен диагностическим. H7 master SPI status может давать false positive, включая `SPI_SR_UDR`, который является slave-TX flag; hot loop не использует его для production recovery.

## Что не использовать в production

| Режим | Почему |
| --- | --- |
| `SPI_STREAM_FW_MAX` | Freerun/max throughput diagnostic |
| `ksps >= 55` | Отмечено как деградирующее ADC/не production |
| `SPI_STREAM_REAL*` | Legacy/diagnostic path, требует prep и не равен FW RR8 |
| `SPI_STREAM_RR8_REAL*` | Diagnostic RR path |
| `SPI_STREAM_RANGE_REAL*` | Diagnostic range path |
| Solo stream перед RR8 как замена | Не является production validation path |
| Grouped/DMA `PATTERN_RUN` | Нарушил бы CS-инвариант |

Validation и host tools описаны в [07_host_tools.md](07_host_tools.md), troubleshooting - в [09_troubleshooting.md](09_troubleshooting.md).
