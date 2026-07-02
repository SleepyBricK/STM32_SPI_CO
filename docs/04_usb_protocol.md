# USB protocol и `RHS1` ABI

USB-интерфейс - vendor-specific bulk device `0483:5741`. Он не CDC и не предоставляет COM-порт. Host tools используют PyUSB и напрямую работают с endpoint `0x01`/`0x81`.

## Interface и endpoints

| Поле | Значение |
| --- | --- |
| VID:PID | `0483:5741` |
| Class | Vendor-specific interface, `bInterfaceClass=0xFF` |
| OUT endpoint | `0x01`, bulk, max packet HS |
| IN endpoint | `0x81`, bulk, max packet HS |
| PCD DMA | Disabled |
| PHY | USB OTG HS через ULPI USB3300 |

OUT endpoint принимает одну текстовую команду до 256 байт. IN endpoint общий для коротких текстовых ответов и бинарных stream frames. Firmware не отправляет текст, если занят frame transfer, и не отправляет frame, если занят текстовый ответ.

## Frame layout

`UsbStreamFrame` строго 4096 байт. `_Static_assert(sizeof(UsbStreamFrame) == 4096)` находится в `Core/Src/usb_stream_frame.c`.

| Offset | Поле | Тип | Размер | Значение |
| ---: | --- | --- | ---: | --- |
| 0 | `magic` | `uint32_t` | 4 | `0x52485331`, ASCII `RHS1` |
| 4 | `version` | `uint16_t` | 2 | `1` |
| 6 | `flags` | `uint16_t` | 2 | Stream flags |
| 8 | `frame_seq` | `uint32_t` | 4 | Номер frame с начала stream |
| 12 | `first_sample_counter` | `uint32_t` | 4 | Первый sample index в frame |
| 16 | `sample_count` | `uint32_t` | 4 | Количество samples в payload |
| 20 | `spi_overflow_count` | `uint32_t` | 4 | Snapshot SPI overflow |
| 24 | `usb_overflow_count` | `uint32_t` | 4 | Snapshot USB overflow |
| 28 | `reserved` | `uint32_t` | 4 | Metadata |
| 32 | `response` | `uint16_t[2032]` | 4064 | ADC/counter payload |

## Flags

| Flag | Значение | Смысл |
| --- | ---: | --- |
| `USB_STREAM_FLAG_COUNTER` | `0x0001` | Payload - counter/synthetic stream |
| `USB_STREAM_FLAG_REAL_ADC` | `0x0002` | Payload - реальные ADC responses |
| `USB_STREAM_FLAG_RR` | `0x0004` | Round-robin/range stream |
| `USB_STREAM_FLAG_CHANNEL_TAG` | `0x0008` | Payload интерпретируется как `uint32_t` tagged words |

Если `CHANNEL_TAG` установлен, максимум samples в frame - 1016, потому что payload читается как 1016 `uint32_t`. Tagged word: bits `[19:16]` - channel, bits `[15:0]` - ADC.

Production `SPI_STREAM_FW n 255 0 40` использует untagged `uint16_t` payload с metadata: каналы восстанавливаются на хосте по `(first_sample_counter + i) % 8`.

## Metadata в `reserved`

`reserved` заполняется макросом `USB_STREAM_META(first_channel, channel_count, convert_flags, channel_bits)`.

| Bits | Поле | Значение |
| --- | --- | --- |
| `[7:0]` | `first_channel` | Первый канал range/RR stream |
| `[15:8]` | `channel_count` | Количество каналов |
| `[23:16]` | `convert_flags` | Flags, переданные в `CONVERT` |
| `[26:24]` | `channel_bits` | Ширина channel tag: 0/2/3/4 |
| `[31:27]` | Reserved | Не используется |

Для production RR8 ожидается `first_channel=0`, `channel_count=8`, `channel_bits=0`.

## Ring и overflow semantics

Frame ring состоит из 32 `UsbStreamFrame` в `.dma_buffer`, выровненных на 32 байта. D2 SRAM помечена MPU как non-cacheable, чтобы SPI/USB data paths не требовали D-cache maintenance.

| Ситуация | Поведение |
| --- | --- |
| Нет свободного frame | `usb_overflow_count++`, samples могут быть dropped |
| Ready FIFO full | frame освобождается, `usb_overflow_count++` |
| USB transmit fail | frame возвращается в начало ready FIFO; если не удалось - overflow |
| Active frame при STOP | endpoint abort + ring reset после EOT |

`spi_overflow_count` и `usb_overflow_count` в header - snapshot на момент открытия frame. Актуальная строка диагностики доступна через `STATS`.

## Text и binary на одном IN endpoint

Host должен различать ответы по длине/формату:

| Тип | Признак |
| --- | --- |
| Text reply | Короткая ASCII-строка, обычно `<512` байт, заканчивается `\n` |
| `RHS1` frame | Ровно 4096 байт, magic `RHS1` |

Во время активного stream команда `STATS` или `STOP` может конкурировать с frame-потоком. Host helper `read_text_during_stream()` читает IN endpoint до первого короткого ASCII-ответа, пропуская бинарные frames.

См. справочник команд в [05_commands.md](05_commands.md) и host helpers в [07_host_tools.md](07_host_tools.md).
