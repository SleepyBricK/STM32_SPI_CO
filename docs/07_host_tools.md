# Host tools

Инструменты в `tools/` рассчитаны на USB Vendor Bulk `0483:5741` и обычно используют PyUSB. Базовые константы и декодер `RHS1` находятся в `tools/usb_intan_lib.py`.

## Базовая библиотека

| Файл | Назначение |
| --- | --- |
| `usb_intan_lib.py` | VID/PID, endpoints, open/reset/drain, text command helpers |
| `validate_rhs1_frame()` | Проверка magic/version/sample_count/frame sequence |
| `iter_rhs1_fw_samples()` | Декодирование RR8 FW frames, tagged и untagged |
| `Rhs1FwDecodeState` | Состояние sequence decode и подсчёт gaps |

Production RR8 frames обычно untagged, поэтому host декодер использует `first_sample_counter` и `n_ch=8`.

## Команды и smoke tests

| Инструмент | Пример | Назначение |
| --- | --- | --- |
| `usb_intan_cmd.py` | `python3 tools/usb_intan_cmd.py PING` | Отправить одну текстовую команду |
| `usb_frame_bench.py` | `python3 tools/usb_frame_bench.py -n 50000 --runs 5 --no-reset` | Проверить `SYNTH_STREAM`/legacy frames |
| `usb_spi_rr8_bench.py` | `python3 tools/usb_spi_rr8_bench.py --rate` | RR8 SPI diagnostics |
| `usb_frame_channel_tag.py` | `python3 tools/usb_frame_channel_tag.py --no-reset` | Проверить tagged payload/range |
| `usb_intan_scan_range.py` | `python3 tools/usb_intan_scan_range.py --first 0 --count 8` | Быстрый range scan |
| `intan_stream_verify.py` | `python3 tools/intan_stream_verify.py --ch 2` | Сравнение stream vs `CONVERT` |

Базовая последовательность проверки:

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
```

## Production capture tools

| Инструмент | Назначение |
| --- | --- |
| `ch_fw_long_suite.py` | Длинный RR8 capture + ch2 solo comparison + PNG/summary |
| `ch_fw_record_csv.py` | RR8 `SPI_STREAM_FW` в CSV и графики |
| `ch_fw_live_plot.py` | Live viewer для RR8 |
| `ch_fw_channel_scan.py` | Health + 8-channel scan |
| `ch_fw16_plot.py` | Короткий plot all-8 channels |
| `ch_fw_rate_sweep.py` | Sweep diagnostic rates |
| `ch_fw_70_bench.py` | High-rate bench, не production |
| `ch_fw_max_bench.py`, `ch_fw_max_suite.py` | `SPI_STREAM_FW_MAX` diagnostics |

Валидированный smoke для production:

```bash
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
```

`ch_fw_long_suite.py` делает prep через `STOP`, `INIT_RECORD 350000`, `CLEAR_ADC`, затем ch2 solo и RR8 capture. Результаты и графики пишутся рядом в `tools/`, например `ch_fw_long_10s_stats.txt`, `ch_fw8_10s_40ksps.png`, `ch_fw8_10s_40ksps_rms.png`.

## Moku/logic analyzer tools

| Инструмент | Назначение |
| --- | --- |
| `moku_fw_rr8_record.py` | Moku waveform generator + RR8 record |
| `moku_fw_sin_scan.py` | Sweep sine frequency into FW stream |
| `moku_fw_wg_record.py` | Square/sine/DC stimulus and RR8 record |
| `moku_la_intan_spi.py` | Moku logic analyzer для SPI |
| `moku_la_spi_hires.py` | High-resolution SPI capture |
| `moku_la_spi_line_check.py` | Проверка SPI-линий во время DMA stream |
| `moku_la_stream_diag.py` | SPI diagnostics during stream |
| `dslogic_intan_spi.py` | DSLogic/sigrok capture and decode |

Эти инструменты зависят от внешнего оборудования и не входят в минимальный validation path.

## Stim/impedance tools

| Инструмент | Назначение |
| --- | --- |
| `pattern_scope_test.py` | Тест stim pattern + Moku scope |
| `pattern_sweep_180ua.py` | Sweep stim ch2/current/on-time |
| `pattern_sweep_scope.py` | Scope capture для stim sweep |
| `test_pattern_timing.py` | Timing smoke для pattern commands |

См. [intan_stim_pattern_guide.md](../intan_stim_pattern_guide.md), [intan_pattern_testing_guide.md](../intan_pattern_testing_guide.md) и [intan_impedance_guide.md](../intan_impedance_guide.md).

## Ожидаемые `STATS`

Для production capture ожидаются:

| Поле | Ожидание |
| --- | --- |
| `sysclk_mhz` | `480` |
| `sck_khz` | около `25000` |
| `pscl` | `8` |
| `nss_midi` | `4` |
| `sample_clip` | `0` |
| `usb_ovf` | `0` |
| `fw_dma_err` | `0` |
| `usb_disconnect` | без роста во время capture |

Для USB HS отдельно проверьте:

```bash
lsusb -t
```

Ожидается `480M`, не `12M`.
