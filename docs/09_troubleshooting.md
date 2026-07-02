# Troubleshooting

## Быстрая диагностика

1. Проверить устройство:

```bash
python3 tools/usb_intan_cmd.py PING
```

2. Проверить статистику:

```bash
python3 tools/usb_intan_cmd.py STATS --no-reset
```

3. Проверить USB speed:

```bash
lsusb -t
```

Для production ожидаются `sysclk_mhz=480`, `sck_khz=25000`, `pscl=8`, `nss_midi=4`, `sample_clip=0`, `usb_ovf=0`, `fw_dma_err=0`, USB `480M`.

## `usb_ovf` растёт

`usb_ovf` означает, что producer не смог получить/поставить frame в USB ring/FIFO. Возможные причины:

| Причина | Что проверить |
| --- | --- |
| USB работает Full Speed | `lsusb -t`, должно быть `480M` |
| Host не читает IN endpoint достаточно быстро | Использовать production tools, не держать stream без reader |
| Активный text reply мешает frame flow | Во время stream минимизировать команды кроме `STOP`/контролируемого `STATS` |
| Disconnect/reset | Смотреть `usb_disconnect` в `STATS` |

После overflow остановите stream через `STOP`, сбросьте host reader и повторите capture.

## `sample_clip` растёт

В FW path `sample_clip` увеличивается не только при аналоговом clipping, но и при внутренних clip/drop ситуациях, например переполнении FW USB queue или tick overlap в timer-paced diagnostic path.

Для production RR8:

| Проверка | Ожидание |
| --- | --- |
| Команда | `SPI_STREAM_FW n 255 0 40` |
| `ksps` | `40`, не high-rate |
| `usb_ovf` | `0` |
| `fw_dma_err` | `0` |

Если analog input подозрителен, повторите sanity test на канале с известной нагрузкой, например ch2 на GND через 10 kOhm.

## `fw_dma_err` растёт

`fw_dma_err` увеличивается только при timeout FW DMA sequence: EOT не пришёл за 1 ms DWT deadline. Это fatal condition для stream; watchdog refresh прекращается.

Проверьте:

| Область | Что проверить |
| --- | --- |
| SPI2 pins | PA9/PB14/PC1, отсутствие конфликтов |
| CS | `PA11/SPI2_NSS`, hardware NSS включён |
| Timing | `pscl=8`, `nss_midi=4`, `sck_khz=25000` |
| DMA ownership | Нет параллельных diagnostic stream/pattern операций |
| STOP/disconnect | Нет роста `usb_disconnect` |

Важно: не используйте `SPI_SR_UDR` как production error. На H7 master status он может давать false positive; hot loop опирается на EOT/deadline.

## `fw_late_seq` растёт

`fw_late_seq` означает DWT phase resync: sequence стартовала позже ожидаемой фазы. Небольшой рост при тяжёлой host/USB нагрузке указывает на latency pressure.

Действия:

| Шаг | Цель |
| --- | --- |
| Проверить `usb_ovf` | Если растёт, сначала лечить USB |
| Проверить CPU load/debug | Не держать breakpoints/долгие операции в hot loop |
| Вернуться к `ksps=40` | High-rate режимы не production |

## `ERR busy` на `NSS_MIDI` или `SPI_PSCL`

Это нормальная защита. Во время active/armed FW stream менять SPI timing нельзя. Выполните:

```bash
python3 tools/usb_intan_cmd.py STOP --no-reset
python3 tools/usb_intan_cmd.py NSS_MIDI 4 --no-reset
python3 tools/usb_intan_cmd.py SPI_PSCL 8 --no-reset
```

В production обычно не нужно вызывать эти команды вручную: `SPI_STREAM_FW ... 255 ... 40` сам восстанавливает `PSCL=8`, `MIDI=4`.

## USB disconnect/reset

Рост `usb_disconnect` в `STATS` означает, что firmware видела USB reset/disconnect. Teardown откладывается в main, активный frame transfer abort'ится до reset ring. Watchdog не refresh'ится во время disconnect teardown.

Проверьте кабель, питание, USB hub, `lsusb -t`, host logs и то, что устройство не просаживается при SPI/stim нагрузке.

## CS violations

Симптомы: неверные `READ`/`ID`, странные ADC values, нестабильные stim patterns, logic analyzer показывает длинный CS low на несколько 32-bit words.

Правильный текущий CS - `PA11/SPI2_NSS`. PE11 - legacy only при `INTAN_CS_HW_NSS=OFF`.

Проверки:

| Проверка | Ожидание |
| --- | --- |
| CMake | `-DINTAN_CS_HW_NSS=ON` |
| SPI2 NSS pin | PA11 AF5 SPI2_NSS, pull-up |
| `Intan_Xfer32Word()` | Один word - одна транзакция |
| Patterns | Не группировать slots под одним CS |

## `ERR spi not ready` или `ERR no intan hw`

`ERR no intan hw` означает сборку без `WITH_INTAN_HW=ON`. `ERR spi not ready` означает, что firmware с Intan path собрана, но SPI/bringup не готов.

Проверьте build options, питание RHS2116, SPI lines, HSE/clock старт и отсутствие конфликтов на pins.

## Плохие данные при `SPI_STREAM_REAL*`

`SPI_STREAM_REAL*`, RR/range и slot-DMA команды являются diagnostic/legacy. Перед ними требуется record prep и priming; firmware частично делает prep внутри некоторых команд, но эти режимы не являются production reference.

Для проверки production качества используйте:

```bash
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
```

и команду `SPI_STREAM_FW n 255 0 40`.
