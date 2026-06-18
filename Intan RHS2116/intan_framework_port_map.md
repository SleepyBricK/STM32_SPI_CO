# Карта: Intan Framework v1.2 → WeActSTM32H743

Оригинальный код (в репозитории):

```text
Intan RHS2116/Intan_RHD_RHS_STM32_Framework_v1_2/H7/rhs_acquisition/
```

PDF: https://intantech.com/files/Intan_RHD_RHS_STM32_Framework.pdf  
Конспект: [intan_rhd_rhs_stm32_framework_notes.md](./intan_rhd_rhs_stm32_framework_notes.md)

> Рекомендуется скопировать или symlink каталог framework в репозиторий (например `Intan RHS2116/ref/`), **без** коммита `Drivers/` и `Debug/` — только `Core/`.

---

## Эталонный проект для RHS2116

| Intan (H7) | Назначение |
|------------|------------|
| `H7/rhs_acquisition/Core/Src/rhsinterface.c` | SPI DMA, sample timer, pipeline, SampleClip |
| `H7/rhs_acquisition/Core/Src/userfunctions.c` | Unpack MISO → memory / USART |
| `H7/rhs_acquisition/Core/Inc/userconfig.h` | 16 CONVERT + 4 AUX, sample rate |
| `H7/rhs_acquisition/Core/Inc/rhsregisters.h` | `convert_command`, `write_command`, … |
| `H7/rhs_acquisition/Core/Src/main.c` | Init, SPI3 NSS pulse, MIDI=6 |
| `H7/rhs_acquisition/rhs_acquisition.ioc` | Cube: TIM3, SPI3, DMA |

---

## Наш проект

| Intan | WeActSTM32H743 |
|-------|----------------|
| `rhsinterface.c` → `transfer_sequence_spi_dma()` | `Core/Src/intan_spi.c` → `intan_dma_prepare_streams_ex`, `Intan_StreamDmaStartSingle` |
| `sample_processing_routine()` | `usb_spi_stream_process()` → `Intan_ConvertPipelineDmaTimSlotRead()` |
| `spi_txrx_cplt_callback()` | `Intan_StreamDmaComplete()` → `intan_stream_dma_unpack()` |
| `command_sequence_MISO[ch + 2]` | `s_dma_rx_words[i + rx_offset]`, `(w>>16)&0xFFFF` |
| `write_data_to_memory()` | `IntanStream_PushBlock()` → USB RHS1 ring |
| `transmit_data_realtime()` | `UsbStreamService_TxPump()` (USB bulk, не USART) |
| `SampleClip` (`TRANSFER_WAIT`) | `Intan_GetSampleClipCount()` / STATS `sample_clip=` |
| `create_convert_sequence()` | `intan_fill_single_tx_words()` / `intan_fill_rr_range_tx_words()` |
| SPI3 + NSS pulse + MIDI 6 | SPI2 + PA11 NSS (`INTAN_CS_HW_NSS=1`) + `Intan_StreamMidiHal()` |

---

## Pipeline +2 (ключевое)

Intan (`userfunctions.c`, offline path):

```c
sample_memory[...] = command_sequence_MISO[FIRST_SAMPLED_CHANNEL + i + 2];
```

Внутри **одной** sequence (20 слов): CONVERT channel `C` → MISO index **`C + 2`**.

Compliance с переносом на **следующую** sequence (`rhsinterface.c`, `locate_compliance_result()`):

- слот `command_slot + 2 >= 20` → результат в **начале** следующей sequence;
- это аналог нашего **hot sub-block** с `rx_offset=0`.

Наш single-channel burst (не 20 слотов, а `n+2` CONVERT):

| Режим | TX слотов | Unpack offset |
|-------|-----------|---------------|
| cold (первый sub-block) | n + 2 | 2 |
| hot (продолжение burst) | n + 2 | 0 |

RR/range (2× CONVERT(63) prime на cold):

| Режим | TX слотов | Unpack offset |
|-------|-----------|---------------|
| cold | 2 prime + n + 2 tail | 4 |
| hot | n + 2 tail | 0 |

---

## SPI: совпадения и отличия

| Параметр | Intan `rhs_acquisition` | Наш код |
|----------|-------------------------|---------|
| DataSize | 32 bit | 32 bit ✓ |
| NSS | `SPI_NSS_PULSE_ENABLE` | `INTAN_CS_HW_NSS=1` (PA11) ✓ |
| MasterInterDataIdleness | **6 cycles** | **6 cycles** (дефолт после выравнивания) |
| SCK | ~25 MHz (из .ioc) | PLL2P 200 MHz / pscl 8 ≈ 25 MHz ✓ |
| Sequence / sample period | **TIM3 → 20 слов / tick** | **USB cmd → один burst n×CONVERT(ch)** |
| AUX slots | 4 (stim/compliance) | нет в stream (только CONVERT) |

Главное **архитектурное** отличие: Intan не шлёт `n` одинаковых CONVERT под одним USB-командой — каждый sample period = **фиксированная** DMA sequence (16+4), timer-driven, callback unpack.

---

## План портирования (без платы)

### Сделано

- [x] CS↑ между 32-bit словами (HW NSS, LA OK)
- [x] Pipeline +2, hot `rx_offset=0`
- [x] Unpack: upper 16 bit only
- [x] `sample_clip` в STATS
- [x] `tools/intan_stream_verify.py`

### Следующий шаг (по framework)

1. **Режим `INTAN_FW_SEQUENCE`** — опциональный path:
   - MOSI buffer = 16 CONVERT + 0..4 AUX (для ch2-only: `CONVERT_COMMANDS=1`, AUX=0);
   - TIM-driven start (или тот же rate через расчёт slot time);
   - unpack `MISO[ch+2]` как Intan.
2. Сверить **Reg init** с `write_initial_reg_values()` / `set_default_rhs_settings()`.
3. При необходимости — **compliance/stim** AUX slots из `AUTO_STIM_CMD_MODE`.

### Проверка с платой

```bash
STM32_Programmer_CLI ... -w build480/WeActSTM32H743.elf -v -rst
python3 tools/intan_stream_verify.py --no-reset --ch 2
python3 tools/usb_intan_cmd.py STATS --no-reset   # sample_clip=0, rx_off=2 или 0
```

---

## Полезные фрагменты Intan для копирования логики

```text
convert_command()          → rhsregisters.h:198
result_is_delayed()        → rhsinterface.c:67   (slot+2 >= 20)
sample_processing_routine  → rhsinterface.c:299  (SampleClip check)
write_data_to_memory       → userfunctions.c:53  (MISO[ch+2])
initialize_spi_with_dma    → rhsinterface.c:361  (MIDI=6)
```
