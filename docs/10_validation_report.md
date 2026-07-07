# Полный отчёт по валидации

Этот документ сводит аппаратные, host-side и диагностические тесты проекта WeActSTM32H743 по состоянию на финальную документационную фиксацию `f9afde4`. Аппаратный финальный ретест выполнялся на firmware `git=5ab5ff9eb410` в Release-HW сборке.

Основные источники: [AGENTS.md](../AGENTS.md), [README.md](../README.md), разделы [01](01_overview.md)..[09](09_troubleshooting.md), [tools/testing](../tools/testing/), host scripts в [tools](../tools/) и специализированные гайды [stim](../intan_stim_pattern_guide.md), [pattern testing](../intan_pattern_testing_guide.md), [impedance](../intan_impedance_guide.md).

## 1. Резюме и вердикт

Production acquisition path валидирован на железе для команды:

```text
SPI_STREAM_FW n 255 0 40
```

Это RR8, каналы `0..7`, 40 kS/s/ch, SPI2 около 25 MHz, аппаратный CS `PA11/SPI2_NSS`, USB HS Vendor Bulk `0483:5741`, `RHS1` frames по 4096 байт. Подробности production path описаны в [06_acquisition.md](06_acquisition.md), USB ABI - в [04_usb_protocol.md](04_usb_protocol.md), аппаратная часть - в [02_hardware.md](02_hardware.md).

| Пункт | Вердикт |
| --- | --- |
| Production RR8 @ 40 kS/s/ch | PASS на аппаратном финальном ретесте |
| Финальный аппаратный ретест | `2026-06-24`, Release-HW, fixture `ch2 -> 1 kOhm -> GND` |
| Firmware во время ретеста | `git=5ab5ff9eb410` |
| Финальный commit документации/host decode | `f9afde4` |
| Основной 30 s capture | `clip=0`, `usb_ovf=0`, `fw_dma_err=0`, `samples_dropped=0` |
| Reliability counters | `iwdg_reset=0`, `last_fault=0` |
| Timing fingerprint | `sysclk_mhz=480`, `sck_khz=25000`, `pscl=8`, `nss_midi=4` |
| ch2 на 1 kOhm, финальный 30 s | `RMS=75.7 µV`, `med=-24.6 µV` после 0.5 s warmup skip |
| USB disconnect handling | PASS: счетчик `usb_disconnect` растет `0 -> 1` при host bus reset |

Диагностические и legacy пути сохранены для отладки, но не заменяют production validation path. Это относится к `SPI_STREAM_REAL*`, `SPI_STREAM_RR8_REAL*`, `SPI_STREAM_RANGE_REAL*`, `SPI_STREAM_FW_MAX`, high-rate `ksps >= 55` и single-channel solo comparison.

## 2. Методология и стенд

### 2.1. Плата и сборка

| Параметр | Значение |
| --- | --- |
| MCU | STM32H743VIT6, Cortex-M7, LQFP100 |
| HSE | 8 MHz |
| SYSCLK | 480 MHz, `BOARD_SYSCLK_480=ON` |
| SPI2 kernel | PLL2P = 200 MHz |
| SPI2 production SCK | 25 MHz, `PSCL=8` |
| CS | `PA11/SPI2_NSS`, hardware pulsed NSS, active low |
| USB | OTG HS + ULPI USB3300, Vendor Bulk `0483:5741` |
| Endpoints | OUT `0x01`, IN `0x81` |
| Build | Release-HW, `WITH_INTAN_HW=ON`, `INTAN_CS_HW_NSS=ON`, `BOARD_SYSCLK_480=ON` |

Сборка и прошивка соответствуют [02_hardware.md](02_hardware.md) и [08_development.md](08_development.md):

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

### 2.2. Fixtures и численные константы

| Fixture / константа | Где использовалось | Значение |
| --- | --- | --- |
| `ch2 -> 1 kOhm -> GND` | phase2/phase3/full retest, 30 s и 15 s RR8 captures | Основной стенд production validation |
| `ch2 -> 10 kOhm -> GND/REF` | исторический анализ `intan_rhs2116_ch2_analysis.md` | Legacy/GUI path, не production |
| `ch4 -> 10 kOhm -> GND/REF` | исторический анализ `intan_rhs2116_ch4_analysis.md` | Legacy/GUI path, не production |
| `uv_per_code` | summary/CSV tools | `0.195 µV/code` |
| `adc_mid` | summary/CSV tools | `32768` |
| Prep для FW capture | `ch_fw_long_suite.py`, `ch_fw_record_csv.py`, `ch_fw_channel_scan.py` | `STOP`, `INIT_RECORD 350000`, `CLEAR_ADC` |
| Warmup skip в summary | `ch_fw_record_csv.py` | 0.5 s для summary/графиков; CSV содержит полный capture |

USB должен быть High Speed. Проверка описана в [07_host_tools.md](07_host_tools.md) и [09_troubleshooting.md](09_troubleshooting.md):

```bash
lsusb -t
```

Ожидание: `480M`, не `12M`.

### 2.3. Формат данных

`UsbStreamFrame` строго 4096 байт: 32-байтный header и payload `uint16_t[2032]` для untagged frames. Production RR8 использует untagged payload, а каналы восстанавливаются на хосте как:

```text
channel = (first_sample_counter + sample_index_in_frame) % 8
```

Хостовый decode реализован в `tools/usb_intan_lib.py` через `validate_rhs1_frame()`, `Rhs1FwDecodeState` и `iter_rhs1_fw_samples()`.

## 3. Критерии PASS/FAIL

### 3.1. Production RR8 критерии

| Поле / проверка | PASS | FAIL / действие | Источник |
| --- | --- | --- | --- |
| Команда | `SPI_STREAM_FW n 255 0 40` | Любая подмена diagnostic path не считается production | [06_acquisition.md](06_acquisition.md) |
| `build_type` | `Release` | Debug/unknown не принимать как финальный hardware verdict | `STATS`, reports |
| `git` | известный short hash | `unknown` не принимать как traceable validation | `STATS`, `phase2_hw_test.py` |
| `sysclk_mhz` | `480` | Проверить clocks/build flags | [02_hardware.md](02_hardware.md) |
| `sck_khz` | `25000` | Проверить SPI2 kernel/PSCL | [02_hardware.md](02_hardware.md) |
| `pscl` | `8` | Production stream должен принудительно вернуть `8` | [06_acquisition.md](06_acquisition.md) |
| `nss_midi` | `4` | Production stream должен принудительно вернуть `4` | [06_acquisition.md](06_acquisition.md) |
| `sample_clip` | `0` | FAIL для production capture | reports/summary |
| `usb_ovf` | `0` | FAIL: producer/ring/USB не успевает | [09_troubleshooting.md](09_troubleshooting.md) |
| `fw_dma_err` | `0` | FAIL: unrecovered 1 ms FW DMA deadline | [06_acquisition.md](06_acquisition.md) |
| `samples_dropped` | `0` | FAIL: потеря samples | [03_architecture.md](03_architecture.md) |
| `iwdg_reset` | `0` после финального retest | FAIL для финального reliability verdict | phase3/full reports |
| `last_fault` | `0` | FAIL: fault handler зафиксировал отказ | phase3/full reports |
| `RHS1 strict` | magic/version/seq/sample_count OK | FAIL: stream ABI или sequence повреждены | `phase2_hw_test.py` |
| STOP during RR8 | `OK`, `fw_dma_err=0` | FAIL: teardown ломает DMA/USB | `phase2_hw_test.py` |
| USB disconnect counter | Рост `0 -> 1` при reset/disconnect test | FAIL/SKIP, если reconnect не подтвержден | `phase2_hw_test.py` |

### 3.2. Diagnostic-only поля и пути

| Поле / путь | Как трактовать |
| --- | --- |
| `fw_late_seq` | DWT phase resync counter. Сам по себе не равен потере данных, если `usb_ovf=0`, `sample_clip=0`, `samples_dropped=0`, `fw_dma_err=0`. |
| Solo `SPI_STREAM_FW n 2 0 40` | Legacy/diagnostic comparison. Solo clipping в long suite не является production FAIL для RR8. |
| `SPI_STREAM_REAL*` | Legacy/diagnostic. Исторически связан с артефактами потока/буферов; не validation reference. |
| `SPI_STREAM_RR8_REAL*`, range streams | Tagged diagnostic paths для сравнения и bench, не production RR8. |
| `SPI_STREAM_FW_MAX` | Freerun/max throughput diagnostic, не production. |
| `ksps >= 55` | Не production, отдельно отмечено как деградирующее. |
| Moku/LA/pattern/impedance tests | Внешняя диагностика аналогового/stim/SPI behavior; не заменяет RR8 production capture. |

## 4. Хронология разработки и валидации

| Commit | Роль в истории | Валидация / артефакт |
| --- | --- | --- |
| `2291262` | Добавлен Framework acquisition path, стабильная RR8 @ 40 kS/s/ch точка | Ранние RR8 captures, переход от legacy путей |
| `6cc82cd` | Укрепление RR8 40 kS/s path и tooling | База для production tooling |
| `fe2c822` | Stream controllable, production SPI settings lock | Проверяется `NSS_MIDI`/`SPI_PSCL` busy lock |
| `0048c4e` | CubeMX `.ioc` синхронизирован с PA11 hardware SPI2_NSS | Аппаратный CS path соответствует [02_hardware.md](02_hardware.md) |
| `757fb09` | Исправлены false-positive FW DMA aborts на H7 master SPI | `phase2_ch2_1k_30s_40ksps_summary.txt`: `fw_dma_err=0` |
| `66e9b65` | Phase 2 observability, USB disconnect handling, strict RHS1 validation | `phase2_hw_test_report.txt`, `phase2_validate_ch2_1k_30s_summary.txt` |
| `3899a49` | Hardware validation phase 2, USB reset трактуется как link-down | `phase3_hw_test_report.txt`, `phase3_validate_ch2_1k_30s_summary.txt` |
| `5ab5ff9` | Phase 3 reliability: IWDG и unified fault handling | `full_retest_report.txt`, финальный 30 s capture |
| `bc736df` | Записаны финальные hardware validation reports | `tools/testing/*report*.txt` |
| `d1890ef` | Конференционный отчёт | [conference_report_ru.md](conference_report_ru.md) |
| `f9afde4` | Полная документация проекта и robust RR8 RHS1 host decode | Текущий documentation/source state |

Фазы разработки:

| Фаза | Что добавлено | Чем подтверждено |
| --- | --- | --- |
| Phase 1 | Production RR8 DWT-paced path, контролируемый 40 kS/s/ch, PSCL/MIDI discipline | `phase1_ch2_1k_30s_40ksps_summary.txt`, `phase2_ch2_1k_30s_40ksps_summary.txt` |
| Phase 2 | Observability, USB disconnect/reset teardown, strict RHS1 validation, SPI timing lock during stream | `phase2_hw_test_report.txt`, `phase2_validate_ch2_1k_30s_summary.txt` |
| Phase 3 | IWDG1, fault counters/blink, `iwdg_reset`, `last_fault`, no watchdog refresh during fatal/disconnect teardown | `phase3_hw_test_report.txt`, `full_retest_report.txt` |

## 5. Каталог тестов

| Скрипт / файл | Тип | Что проверяет | Типичная команда | В production validation path |
| --- | --- | --- | --- | --- |
| `tools/usb_intan_cmd.py` | HW smoke/control | Одна USB text command: `PING`, `STATS`, `STOP`, `READ`, `WRITE` | `python3 tools/usb_intan_cmd.py PING` | Да, preflight |
| `tools/phase2_hw_test.py` | HW automated regression | STATS fingerprint, SPI timing lock, STOP, strict RHS1, USB disconnect counter | `python3 tools/phase2_hw_test.py` | Да |
| `tools/ch_fw_long_suite.py` | HW production + diagnostic compare | 10 s ch2 solo и RR8, per-channel RMS/clip, plots | `python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40` | Да, RR8 часть |
| `tools/ch_fw_record_csv.py` | HW production capture | RR8 CSV, summary, plots, clip check | `python3 tools/ch_fw_record_csv.py --duration 30 --ksps 40` | Да |
| `tools/ch_fw_channel_scan.py` | HW production scan | Health `PING/ID/STATS`, optional solo scan, RR8 per-channel RMS | `python3 tools/ch_fw_channel_scan.py --rr8-s 15 --ksps 40` | Да, diagnostic scan |
| `tools/usb_frame_bench.py` | HW/SW USB ABI | `SYNTH_STREAM`/legacy stream `RHS1` frame header, sequence, counter payload | `python3 tools/usb_frame_bench.py -n 50000 --runs 5 --no-reset` | Да, USB smoke |
| `tools/test_rhs1_skip_decode.py` | SW host-side | Симуляция dropped frames, проверка untagged RR8 channel phase через `first_sample_counter` | `python3 tools/test_rhs1_skip_decode.py` | Да, host regression |
| `tools/usb_frame_channel_tag.py` | HW diagnostic | Tagged `uint32` payload, channel tags, RR/range metadata | `python3 tools/usb_frame_channel_tag.py --mode rr8 --no-reset` | Нет, tagged diagnostic |
| `tools/usb_spi_rr8_bench.py` | HW diagnostic | `SPI_RATE_RR8`, `SPI_TO_RAM_RR8`, RR8 counter/real frame checks | `python3 tools/usb_spi_rr8_bench.py --rate` | Нет |
| `tools/usb_intan_scan_range.py` | HW diagnostic | Range/RR tagged real stream, per-channel simple stats | `python3 tools/usb_intan_scan_range.py --first 0 --count 8` | Нет |
| `tools/intan_stream_verify.py` | HW diagnostic/legacy | `CONVERT` vs `SPI_STREAM_REAL*`, lengths including 8188, clip/rx_off | `python3 tools/intan_stream_verify.py --ch 2 --lengths 256,1024,4096,8188` | Нет |
| `tools/stream_validate_quick.py` | HW legacy regression | Быстрая проверка старого `SPI_STREAM_REAL` ch4 и `SPI_STREAM_RR8_REAL`, включая artifact boundary 8190 | `python3 tools/stream_validate_quick.py` | Нет |
| `tools/run_full_stream_suite.py` | HW legacy suite | Single, RR8 tagged, custom range, correlations, plots, PASS/FAIL | `python3 tools/run_full_stream_suite.py` | Нет |
| `tools/ch_fw_70_bench.py` | HW diagnostic | `SPI_STREAM_FW` high-rate bench; default production 40, но применяется для rate experiments | `python3 tools/ch_fw_70_bench.py --ksps 40` | Нет |
| `tools/ch_fw_rate_sweep.py` | HW diagnostic | Sweep `SPI_STREAM_FW` ksps, clip и RMS | `python3 tools/ch_fw_rate_sweep.py --ksps-list 40` | Нет |
| `tools/ch_fw_max_bench.py` | HW diagnostic | `SPI_STREAM_FW_MAX` freerun throughput, clip/usb_ovf/RMS | `python3 tools/ch_fw_max_bench.py --n 5000 --ch 255` | Нет |
| `tools/ch_fw_max_suite.py` | HW diagnostic | `SPI_STREAM_FW_MAX` capture + plots | `python3 tools/ch_fw_max_suite.py --duration 5` | Нет |
| `tools/ch_fw_live_plot.py` | HW viewer | Live RR8 viewer, STOP during active RHS1 frames via `read_text_during_stream()` | `python3 tools/ch_fw_live_plot.py` | Нет |
| `tools/moku_sin_record_test.py` | External/Moku legacy | Moku sine -> `SPI_STREAM_REAL`, FFT/SNR/correlation | `python3 tools/moku_sin_record_test.py --no-reset` | Нет |
| `tools/moku_fw_sin_scan.py` | External/Moku FW solo | Moku sine frequency scan -> `SPI_STREAM_FW` solo | `python3 tools/moku_fw_sin_scan.py --ch 0 --freqs 10,25,50,100` | Нет |
| `tools/moku_fw_rr8_record.py` | External/Moku FW RR8 | Moku sine -> production-style RR8 record, plots, correlation | `python3 tools/moku_fw_rr8_record.py --duration 5 --ksps 40` | Нет |
| `tools/moku_fw_wg_record.py` | External/Moku FW RR8 | Moku square/sine/DC -> RR8, waveform detection on selected channel | `python3 tools/moku_fw_wg_record.py --waveform square --moku-ch 4` | Нет |
| `tools/moku_sin_speed_sweep.py` | External/Moku diagnostic | Поиск SPI speed settings с хорошей синусоидой для legacy stream | `python3 tools/moku_sin_speed_sweep.py --no-reset` | Нет |
| `tools/moku_la_intan_spi.py` | External/LA | Moku LA визуально/программно проверяет pulsed NSS на SPI | `python3 tools/moku_la_intan_spi.py` | Нет |
| `tools/moku_la_spi_hires.py` | External/LA | High-res SPI capture, CS low width, SCK only under CS | `python3 tools/moku_la_spi_hires.py --pscl 8 --high-res` | Нет |
| `tools/moku_la_spi_line_check.py` | External/LA | CS/SCK/MOSI/MISO line checks during stream | `python3 tools/moku_la_spi_line_check.py --pscl 32` | Нет |
| `tools/moku_la_stream_diag.py` | External/LA | CS aliasing диагностика и 8188/8190 chunk artifact check for legacy stream | `python3 tools/moku_la_stream_diag.py --pscl 16` | Нет |
| `tools/test_pattern_timing.py` | HW stim timing | Wall-clock slope для `PATTERN_ADD_DELAY_US` | `python3 tools/test_pattern_timing.py --no-reset` | Нет |
| `tools/pattern_scope_test.py` | External/Moku stim | Stim pattern + Moku scope, pulse width/amplitude | `python3 tools/pattern_scope_test.py --mode cal --no-reset` | Нет |
| `tools/pattern_sweep_180ua.py` | HW/external stim | Сборка/запуск sweep стим-импульсов ch2 180 µA | `python3 tools/pattern_sweep_180ua.py --no-reset --run 20` | Нет |
| `tools/pattern_sweep_scope.py` | External/Moku stim | Scope capture sweep pulses, measured widths/gaps | `python3 tools/pattern_sweep_scope.py --repeat 20 --no-reset` | Нет |
| `tools/pattern_sweep_lib.py` | SW helper | Генерация command list для stim sweep | Imported helper | Нет |

## 6. Автоматизированная аппаратная валидация

`tools/phase2_hw_test.py` содержит 5 test functions. В отчетах одна функция (`test_spi_lock_idle_and_busy`) дает две наблюдаемые PASS-строки: idle настройка `NSS_MIDI` и busy lock во время RR8 stream.

| Проверка | Что делает | Phase 2 report | Phase 3 report | Full retest |
| --- | --- | --- | --- | --- |
| STATS fingerprint / observability | `build_type=Release`, `git` известен, `pscl=8`, `nss_midi=4`; phase3 поля `iwdg_reset`, `last_fault` присутствуют | PASS: `git=66e9b652b896`, `pscl=8`, `midi=4` | PASS: `iwdg_reset=0`, `last_fault=0`, `build=Release` | PASS: `build=Release`, `pscl=8`, `nss_midi=4`, `iwdg_reset=0`, `last_fault=0` |
| NSS_MIDI idle | После `STOP`, `NSS_MIDI 2` должен вернуть `OK`, затем вернуть `4` | PASS | PASS | PASS |
| SPI lock during stream | Во время `SPI_STREAM_FW ... 255 ... 40` попытка `NSS_MIDI 2`; после STOP `nss_midi=4`, `pscl=8` | PASS: `nss_midi=4`, `pscl=8` | PASS | PASS |
| STOP during RR8 | Старт RR8, чтение frames около 2 s, `STOP`, `fw_dma_err=0` | PASS: `fw_dma_err=0` | PASS | PASS: `fw_dma_err=0` |
| RHS1 strict | `SPI_STREAM_FW 8000 255 0 40`, strict magic/version/frame sequence/sample_count | PASS: 32 frames, magic/version/seq OK | PASS | PASS: 32 frames |
| USB disconnect counter | Stream active, close/reset USB, reconnect, `usb_disconnect` растет | PASS: `0 -> 1` | PASS: `0 -> 1` | PASS: `0 -> 1` |

## 7. Длинные RR8 захваты 30 s

В таблице `n/ch` из summary равен числу samples после 0.5 s warmup skip. Полный CSV для 30 s содержит `1_200_000` rows/channel; summary считает RMS по `1_180_000` samples/channel.

| Summary | UTC | Git | Wall, s | n/ch в summary | clip | usb_ovf | fw_dma_err | samples_dropped | fw_late_seq | iwdg_reset | last_fault | ch2 med, µV | ch2 RMS, µV |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- | ---: | ---: |
| [fw_rr8_30s_40ksps_summary.txt](../tools/testing/fw_rr8_30s_40ksps_summary.txt) | 2026-06-23T14:47:54Z | n/a | 31.001 | 1,180,000 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | 30.0 | 103.9 |
| [phase1_ch2_1k_30s_40ksps_summary.txt](../tools/testing/phase1_ch2_1k_30s_40ksps_summary.txt) | 2026-06-24T11:02:57Z | n/a | 31.075 | 1,180,000 | 0 | 0 | 0 | n/a | n/a | n/a | n/a | -8.2 | 85.8 |
| [phase2_ch2_1k_30s_40ksps_summary.txt](../tools/testing/phase2_ch2_1k_30s_40ksps_summary.txt) | 2026-06-24T11:16:52Z | `757fb09b00ca` | 30.427 | 1,180,000 | 0 | 0 | 0 | 0 | 689 | n/a | n/a | -26.5 | 76.9 |
| [phase2_validate_ch2_1k_30s_summary.txt](../tools/testing/phase2_validate_ch2_1k_30s_summary.txt) | 2026-06-24T11:29:56Z | `66e9b652b896` | 30.771 | 1,180,000 | 0 | 0 | 0 | 0 | 11,538 | n/a | n/a | -25.4 | 75.5 |
| [phase3_validate_ch2_1k_30s_summary.txt](../tools/testing/phase3_validate_ch2_1k_30s_summary.txt) | 2026-06-24T11:46:13Z | `3899a494912b` | 30.465 | 1,180,000 | 0 | 0 | 0 | 0 | 1,690 | 0 | 0 | -25.4 | 77.2 |
| [full_retest_ch2_1k_30s_summary.txt](../tools/testing/full_retest_ch2_1k_30s_summary.txt) | 2026-06-24T11:50:34Z | `5ab5ff9eb410` | 30.449 | 1,180,000 | 0 | 0 | 0 | 0 | 1,738 | 0 | 0 | -24.6 | 75.7 |

Общие поля для всех 30 s captures, когда они присутствуют: `cmd=SPI_STREAM_FW 1200000 255 0 40`, `ksps_per_ch=40`, `channels=0..7`, `samples=9600000`, `frames_out=4725`, `spi_xfer32=9600024`, `sck_khz=25000`, `pscl=8`, `nss_midi=4`.

### 7.1. Per-channel RMS 30 s

| Summary | ch0 | ch1 | ch2 | ch3 | ch4 | ch5 | ch6 | ch7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `fw_rr8_30s_40ksps` | 1995.2 | 4110.3 | 103.9 | 4116.3 | 1058.6 | 3547.8 | 3594.3 | 3967.8 |
| `phase1_ch2_1k_30s_40ksps` | 4099.3 | 4326.9 | 85.8 | 4139.9 | 4749.1 | 4725.6 | 4939.5 | 4130.8 |
| `phase2_ch2_1k_30s_40ksps` | 4205.7 | 4295.6 | 76.9 | 4191.9 | 4877.4 | 4720.4 | 5002.6 | 4209.2 |
| `phase2_validate_ch2_1k_30s` | 4119.4 | 4374.7 | 75.5 | 4143.7 | 4930.5 | 4701.7 | 4929.8 | 4150.6 |
| `phase3_validate_ch2_1k_30s` | 4157.1 | 4458.6 | 77.2 | 4181.7 | 4856.4 | 4706.0 | 5082.0 | 4187.1 |
| `full_retest_ch2_1k_30s` | 4149.1 | 4443.8 | 75.7 | 4180.1 | 4908.3 | 4712.7 | 5094.3 | 4162.1 |

### 7.2. Тренд `fw_late_seq`

`fw_late_seq` отсутствует в ранних summary от `2026-06-23` и phase1. После появления счетчика:

| Capture | `fw_late_seq` | Комментарий |
| --- | ---: | --- |
| `phase2_ch2_1k_30s_40ksps` | 689 | Низкий уровень resync, данные не dropped |
| `phase2_validate_ch2_1k_30s` | 11,538 | В отчете phase2 указано около 0.96% от 1.2M sequences; при этом `clip=0`, `usb_ovf=0`, `fw_dma_err=0`, `samples_dropped=0` |
| `phase3_validate_ch2_1k_30s` | 1,690 | Существенно ниже phase2_validate |
| `full_retest_ch2_1k_30s` | 1,738 | Стабилен около phase3 уровня; production PASS |

Интерпретация соответствует [06_acquisition.md](06_acquisition.md) и [09_troubleshooting.md](09_troubleshooting.md): `fw_late_seq` - это phase resync, а не доказательство потери samples без роста `samples_dropped`/`usb_ovf`/`fw_dma_err`.

## 8. 10 s long suite и solo vs RR8

`ch_fw_long_suite.py` делает prep (`STOP`, `INIT_RECORD 350000`, `CLEAR_ADC`), затем ch2 solo capture и RR8 capture. Solo часть используется только как диагностическое сравнение.

| Артефакт | RR8 результат | Solo результат | Вердикт |
| --- | --- | --- | --- |
| [phase2_hw_test_report.txt](../tools/testing/phase2_hw_test_report.txt) | `ch2 RR8 clip=0`, `RMS=75 µV` | `ch2 solo clip=394` | RR8 PASS; solo legacy diagnostic |
| [full_retest_report.txt](../tools/testing/full_retest_report.txt) | `ch2 RR8 RMS=75 µV`, `clip=0`; all `ch0..ch7` RR8 `clip=0` | `ch2 solo clip=466` | RR8 PASS; solo legacy diagnostic |

Пути графиков из full retest:

| График | Путь |
| --- | --- |
| RR8 10 s overview | [../tools/ch_fw8_10s_40ksps.png](../tools/ch_fw8_10s_40ksps.png) |
| ch2 solo vs RR8 | [../tools/ch2_fw_solo_vs_rr8_10s.png](../tools/ch2_fw_solo_vs_rr8_10s.png) |

## 9. Channel scan 15 s

Все 15 s scan summary выполнены на `git=5ab5ff9eb410`, `cmd=SPI_STREAM_FW 600000 255 0 40`, `clip=0`, `usb_ovf=0`, `fw_dma_err=0`, `samples_dropped=0`, `iwdg_reset=0`, `last_fault=0`, `pscl=8`, `nss_midi=4`. Summary RMS рассчитан после 0.5 s warmup skip: `n=580000` на канал.

| Summary | UTC | Fixture note | Wall, s | fw_late_seq | ch2 med, µV | ch2 RMS, µV | Комментарий |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| [scan_15s_ch2_1k_40ksps_summary.txt](../tools/testing/scan_15s_ch2_1k_40ksps_summary.txt) | 2026-06-26T10:20:22Z | ch2 1 kOhm | 15.330 | 1,220 | 12.9 | 47.7 | PASS |
| [scan_15s_ch2_1k_typec_40ksps_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_summary.txt) | 2026-06-26T10:58:13Z | Type-C cable scan | 15.289 | 1,035 | -5.1 | 32.1 | PASS; самый низкий ch2 RMS в scan set |
| [scan_15s_ch2_1k_typec_40ksps_r2_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r2_summary.txt) | 2026-06-26T11:06:12Z | Type-C cable scan, repeat 2 | 15.282 | 984 | 1.6 | 65.3 | PASS |
| [scan_15s_ch2_1k_typec_40ksps_r3_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r3_summary.txt) | 2026-06-26T11:09:54Z | Type-C cable scan, repeat 3 | 15.273 | 1,006 | -2279.4 | 3852.9 | Аномалия/смена fixture: ch2 перестал выглядеть как нагруженный 1 kOhm; при этом stream counters остаются PASS |

### 9.1. Per-channel RMS 15 s

| Summary | ch0 | ch1 | ch2 | ch3 | ch4 | ch5 | ch6 | ch7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `scan_15s_ch2_1k_40ksps` | 740.3 | 3575.9 | 47.7 | 3767.2 | 3606.1 | 3516.6 | 4713.3 | 3818.1 |
| `scan_15s_ch2_1k_typec_40ksps` | 518.5 | 2718.8 | 32.1 | 3808.3 | 3894.7 | 3599.1 | 3943.1 | 3740.4 |
| `scan_15s_ch2_1k_typec_40ksps_r2` | 4256.8 | 3880.3 | 65.3 | 3791.8 | 4790.3 | 4383.6 | 4586.5 | 3639.3 |
| `scan_15s_ch2_1k_typec_40ksps_r3` | 31.9 | 3723.9 | 3852.9 | 3708.3 | 3967.5 | 3627.9 | 4033.6 | 3507.4 |

`r3` выглядит как смена подключения: ch0 становится очень тихим (`RMS=31.9 µV`), а ch2 получает RMS уровня ненагруженных/шумных каналов. Это не зафиксировано как USB/SPI/firmware failure, потому что `sample_clip=0`, `usb_ovf=0`, `fw_dma_err=0`, `samples_dropped=0`, `iwdg_reset=0`, `last_fault=0`.

## 10. USB/RHS1 host-side tests

| Тест | Проверка | Подтверждение |
| --- | --- | --- |
| `usb_frame_bench.py` | `SYNTH_STREAM` и legacy frame modes: `RHS1` magic/version, `frame_seq`, `first_sample_counter`, overflow snapshots, counter spot checks | Входит в recommended smoke; явного result artifact в `tools/testing/*.txt` нет |
| `phase2_hw_test.py` strict RHS1 | Hardware `SPI_STREAM_FW 8000 255 0 40`: strict magic/version/seq/sample_count | PASS в phase2, phase3 и full retest reports; full retest: strict 32 frames |
| `test_rhs1_skip_decode.py` | Host simulation: dropped `RHS1` frames не должны сдвигать untagged RR8 channel phase | Локальный запуск при подготовке отчета: `PASS: ch0 median=0x7FFE, samples=13208, skipped_frames=12, seq_gaps=12` |
| `usb_intan_lib.py` robust decode | `iter_rhs1_fw_samples()` использует `first_sample_counter`, а не локальный host индекс; `strict_seq=False` считает gaps без channel drift | Зафиксировано в коде текущего `f9afde4` |

Рекомендуемый USB smoke из [07_host_tools.md](07_host_tools.md):

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
python3 tools/test_rhs1_skip_decode.py
```

## 11. Legacy/diagnostic paths и исторические проблемы

### 11.1. Исторический ch2 analysis

Файл: [intan_rhs2116_ch2_analysis.md](../intan_rhs2116_ch2_analysis.md).

| Поле | Значение |
| --- | ---: |
| Fixture | ch2 через 10 kOhm на GND/REF |
| Rows | 3,783,000 |
| RMS весь файл | 22.03 µV |
| RMS после удаления первых 5000 samples | 20.35 µV |
| MAD noise estimate | 13.88 µV |
| Минимум | -578.76 µV |
| Максимум | +809.055 µV |
| Артефакт | отрицательные провалы каждые 8190 samples |
| Header issue | конфликт `nominal_sample_rate_hz=40000`, `measured_sample_rate_hz=363628.861...`, `firmware_adc_rate_hz=350000`, `time_binding_source=recv_wall` |

Вывод анализа: ch2 лучше ch4, но файл не является валидной оценкой аналогового шума, пока не исправлены поток/буферизация/временная модель. Это исторический legacy/GUI/export path, не production RR8 validation.

### 11.2. Исторический ch4 analysis

Файл: [intan_rhs2116_ch4_analysis.md](../intan_rhs2116_ch4_analysis.md).

| Поле | Значение |
| --- | ---: |
| Fixture | ch4 через 10 kOhm на GND/REF |
| Rows | 5,280,876 |
| RMS / STD | 62.07 µV |
| Минимум | -986.7 µV |
| Максимум | +3398.46 µV |
| Peak-to-peak | 4385.16 µV |
| MAD noise estimate | около 20.8 µV |
| Точек выше +/-1000 µV | 490 |
| Артефакт | отрицательные провалы каждые 8190 samples |
| Header issue | `time_binding_source=recv_wall`, measured sample rate не считается надежной физической частотой |

Вывод анализа: данные плохие для закороченного входа и похожи на артефакты потока, парсинга, буфера или синхронизации. Production FW path и `RHS1` host decode были введены, чтобы уйти от этих проблем.

### 11.3. Legacy stream suite

`tools/run_full_stream_suite.py`, `tools/intan_stream_verify.py`, `tools/stream_validate_quick.py`, `tools/usb_frame_channel_tag.py`, `tools/usb_intan_scan_range.py` и `tools/usb_spi_rr8_bench.py` остаются полезными для диагностики:

| Путь | Текущее назначение |
| --- | --- |
| `SPI_STREAM_REAL*` | Проверка старого single-channel ADC streaming, chunk boundaries, artifact regressions |
| `SPI_STREAM_RR8_REAL*` | Tagged RR8 diagnostic stream |
| `SPI_STREAM_RANGE_REAL*` | Tagged range diagnostics |
| `SPI_RATE*`, `SPI_TO_RAM*` | Скоростные и RAM-only сравнения |

Эти пути не должны использоваться как production acceptance вместо `SPI_STREAM_FW n 255 0 40`.

## 12. Стим, импеданс, Moku и LA

Эти проверки важны для полной аппаратной уверенности, но находятся вне production RR8 data path.

| Область | Гайд / скрипты | Что проверяет | Статус относительно production |
| --- | --- | --- | --- |
| Stim pattern | [intan_stim_pattern_guide.md](../intan_stim_pattern_guide.md), [intan_pattern_testing_guide.md](../intan_pattern_testing_guide.md), `test_pattern_timing.py`, `pattern_scope_test.py`, `pattern_sweep_180ua.py`, `pattern_sweep_scope.py` | `PATTERN_ADD_*`, `PATTERN_RUN`, DWT delay, safe OFF, Moku pulse widths | Diagnostic/external |
| Impedance | [intan_impedance_guide.md](../intan_impedance_guide.md) | `IMPEDANCE_MEASURE`, 3-slot write/convert, safe-state Reg2/Reg3, 10 kOhm practical mode | Diagnostic/external |
| Moku waveform injection | `moku_sin_record_test.py`, `moku_fw_sin_scan.py`, `moku_fw_rr8_record.py`, `moku_fw_wg_record.py`, `moku_sin_speed_sweep.py` | Ввод sine/square/DC на Intan channels, FFT/correlation/RMS | Diagnostic/external |
| Logic analyzer | `moku_la_intan_spi.py`, `moku_la_spi_hires.py`, `moku_la_spi_line_check.py`, `moku_la_stream_diag.py` | CS/SCK/MOSI/MISO, pulsed NSS, отсутствие grouped CS burst, aliasing warnings | Diagnostic/external |

Факт из impedance guide: практический режим для 10 kOhm на текущей плате:

```bash
python3 tools/usb_intan_cmd.py INIT_RECORD 610 --no-reset
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 0 1 1000 16 128 3" --no-reset
```

Наблюдавшийся результат в гайде: `overruns=0`, `spi_errors=0`, `clipped=0`, median impedance около `8.5 kOhm`. Это не относится к RR8 production capture, но подтверждает отдельный impedance path.

## 13. Известные ограничения и не проверено

| Ограничение | Статус |
| --- | --- |
| `ksps >= 55` | Не production; в проектном контексте отмечено как деградирующее. |
| `SPI_STREAM_FW_MAX` | Freerun/max throughput diagnostic; не валидирован как production. |
| Solo stream | Может давать `sample_clip` в long suite; не production FAIL для RR8. |
| `SPI_STREAM_REAL*`, RR/range legacy | Сохранены для диагностики; исторически имели проблемы с artifacts/export/timing. |
| Биологическая валидация | Не покрыта представленными артефактами. Тест на резисторе проверяет тракт и поток, но не заменяет эксперимент с тканью/электродами. |
| Влияние кабеля | Есть только 15 s Type-C scan set; систематическое кабельное исследование не выполнено. |
| `scan r3` anomaly | Зафиксировано как вероятная смена fixture/снятая 1 kOhm нагрузка, не как firmware failure. |
| `fw_late_seq` | Нужно отслеживать, но не считать data loss без сопутствующих `usb_ovf`, `samples_dropped`, `fw_dma_err`. |
| USB speed | Production требует USB HS `480M`; Full Speed не валидирован для capture. |
| RHS1 ABI changes | Не менять без синхронного обновления host tools, см. [04_usb_protocol.md](04_usb_protocol.md). |

## 14. Рекомендуемый regression checklist

### 14.1. Минимальный regression после изменения firmware

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build

python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
lsusb -t

python3 tools/phase2_hw_test.py
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
python3 tools/test_rhs1_skip_decode.py
```

Acceptance для минимального набора:

| Проверка | Ожидание |
| --- | --- |
| `PING` | `PONG` |
| `STATS` | `build_type=Release`, `sysclk_mhz=480`, `sck_khz=25000`, `pscl=8`, `nss_midi=4` |
| USB topology | `480M` |
| `phase2_hw_test.py` | All tests passed |
| `ch_fw_long_suite.py` | RR8 `clip=0`, ch2 RR8 RMS около уровня последних ретестов; solo clip не считать production FAIL |
| `test_rhs1_skip_decode.py` | PASS |

### 14.2. Полный regression перед релизом или изменением clocks/SPI/USB/acquisition

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
python3 tools/test_rhs1_skip_decode.py

python3 tools/phase2_hw_test.py
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
python3 tools/ch_fw_record_csv.py --duration 30 --ksps 40 \
  -o tools/testing/<new_retest_stem>.csv
python3 tools/ch_fw_channel_scan.py --rr8-s 15 --ksps 40 \
  -o tools/testing
```

Дополнительно по области изменения:

| Если менялось | Добавить |
| --- | --- |
| `RHS1` ABI / host decode | `usb_frame_channel_tag.py`, `usb_intan_scan_range.py`, review [04_usb_protocol.md](04_usb_protocol.md) |
| SPI/CS timing | Moku/LA checks: `moku_la_spi_hires.py`, `moku_la_spi_line_check.py`; сверить [02_hardware.md](02_hardware.md) |
| Legacy stream code | `intan_stream_verify.py`, `stream_validate_quick.py`, `run_full_stream_suite.py` |
| FW rate/pacing | `ch_fw_rate_sweep.py`, `ch_fw_70_bench.py`, но production verdict только на 40 kS/s/ch |
| Stim pattern | `test_pattern_timing.py`, `pattern_scope_test.py`, [intan_pattern_testing_guide.md](../intan_pattern_testing_guide.md) |
| Impedance | `IMPEDANCE_MEASURE` procedure из [intan_impedance_guide.md](../intan_impedance_guide.md) |
| Документация | Обновить [AGENTS.md](../AGENTS.md), [README.md](../README.md), [01_overview.md](01_overview.md), [06_acquisition.md](06_acquisition.md), [07_host_tools.md](07_host_tools.md), [09_troubleshooting.md](09_troubleshooting.md) по затронутой области |

## 15. Приложения

### 15.1. Отчеты

| Файл | Содержание |
| --- | --- |
| [full_retest_report.txt](../tools/testing/full_retest_report.txt) | Финальный hardware retest `2026-06-24`, `git=5ab5ff9eb410` |
| [phase2_hw_test_report.txt](../tools/testing/phase2_hw_test_report.txt) | Phase 2 hardware validation, `git=66e9b652b896` |
| [phase3_hw_test_report.txt](../tools/testing/phase3_hw_test_report.txt) | Phase 3 hardware validation, `git=3899a494912b` |
| [conference_report_ru.md](conference_report_ru.md) | Неформальный отчет для конференции |

### 15.2. 30 s summary files

| Файл | Назначение |
| --- | --- |
| [fw_rr8_30s_40ksps_summary.txt](../tools/testing/fw_rr8_30s_40ksps_summary.txt) | Ранний RR8 30 s capture, `2026-06-23`, ch2 RMS 103.9 µV |
| [phase1_ch2_1k_30s_40ksps_summary.txt](../tools/testing/phase1_ch2_1k_30s_40ksps_summary.txt) | Phase 1 30 s capture, no `fw_late_seq` field |
| [phase2_ch2_1k_30s_40ksps_summary.txt](../tools/testing/phase2_ch2_1k_30s_40ksps_summary.txt) | `git=757fb09b00ca`, `fw_late_seq=689` |
| [phase2_validate_ch2_1k_30s_summary.txt](../tools/testing/phase2_validate_ch2_1k_30s_summary.txt) | `git=66e9b652b896`, `fw_late_seq=11538` |
| [phase3_validate_ch2_1k_30s_summary.txt](../tools/testing/phase3_validate_ch2_1k_30s_summary.txt) | `git=3899a494912b`, `fw_late_seq=1690` |
| [full_retest_ch2_1k_30s_summary.txt](../tools/testing/full_retest_ch2_1k_30s_summary.txt) | Финальный 30 s retest, `git=5ab5ff9eb410`, `fw_late_seq=1738` |

### 15.3. 15 s scan summary files

| Файл | Назначение |
| --- | --- |
| [scan_15s_ch2_1k_40ksps_summary.txt](../tools/testing/scan_15s_ch2_1k_40ksps_summary.txt) | 15 s RR8 scan, ch2 RMS 47.7 µV |
| [scan_15s_ch2_1k_typec_40ksps_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_summary.txt) | Type-C scan, ch2 RMS 32.1 µV |
| [scan_15s_ch2_1k_typec_40ksps_r2_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r2_summary.txt) | Type-C repeat 2, ch2 RMS 65.3 µV |
| [scan_15s_ch2_1k_typec_40ksps_r3_summary.txt](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r3_summary.txt) | Type-C repeat 3 anomaly, ch2 RMS 3852.9 µV |

### 15.4. Графики

| Группа | Пути |
| --- | --- |
| Финальный 30 s retest | [full_retest_ch2_1k_30s_8ch.png](../tools/testing/full_retest_ch2_1k_30s_8ch.png), [full_retest_ch2_1k_30s_rms.png](../tools/testing/full_retest_ch2_1k_30s_rms.png), [full_retest_ch2_1k_30s_ch2_zoom1s.png](../tools/testing/full_retest_ch2_1k_30s_ch2_zoom1s.png), [full_retest_ch2_1k_30s_all_ch_zoom1s.png](../tools/testing/full_retest_ch2_1k_30s_all_ch_zoom1s.png) |
| Phase 2 validation | [phase2_validate_ch2_1k_30s_8ch.png](../tools/testing/phase2_validate_ch2_1k_30s_8ch.png), [phase2_validate_ch2_1k_30s_rms.png](../tools/testing/phase2_validate_ch2_1k_30s_rms.png), [phase2_validate_ch2_1k_30s_ch2_zoom1s.png](../tools/testing/phase2_validate_ch2_1k_30s_ch2_zoom1s.png) |
| Phase 3 validation | [phase3_validate_ch2_1k_30s_8ch.png](../tools/testing/phase3_validate_ch2_1k_30s_8ch.png), [phase3_validate_ch2_1k_30s_rms.png](../tools/testing/phase3_validate_ch2_1k_30s_rms.png), [phase3_validate_ch2_1k_30s_ch2_zoom1s.png](../tools/testing/phase3_validate_ch2_1k_30s_ch2_zoom1s.png) |
| 15 s scans | [scan_15s_ch2_1k_40ksps_8ch.png](../tools/testing/scan_15s_ch2_1k_40ksps_8ch.png), [scan_15s_ch2_1k_typec_40ksps_8ch.png](../tools/testing/scan_15s_ch2_1k_typec_40ksps_8ch.png), [scan_15s_ch2_1k_typec_40ksps_r2_8ch.png](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r2_8ch.png), [scan_15s_ch2_1k_typec_40ksps_r3_8ch.png](../tools/testing/scan_15s_ch2_1k_typec_40ksps_r3_8ch.png) |
| 10 s suite | [ch_fw8_10s_40ksps.png](../tools/ch_fw8_10s_40ksps.png), [ch_fw8_10s_40ksps_rms.png](../tools/ch_fw8_10s_40ksps_rms.png), [ch2_fw_solo_vs_rr8_10s.png](../tools/ch2_fw_solo_vs_rr8_10s.png) |

