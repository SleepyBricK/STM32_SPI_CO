# WeActSTM32H743

Firmware for a custom **STM32H743VIT6** board with USB HS Vendor Bulk streaming and an **Intan RHS2116** on SPI2. The firmware streams fixed-size `RHS1` frames to a host over USB and provides text commands over the same vendor interface.

The development context, board constraints, and production operating mode live in [AGENTS.md](AGENTS.md).

## Production mode

The validated acquisition command is:

```text
SPI_STREAM_FW <samples-per-channel> 255 0 40
```

It captures channels `0..7` (RR8) at **40 kS/s/ch**. A missing or zero `ksps` argument resolves to the same safe default. This mode uses the DWT phase-paced acquisition path and is the only production configuration. A production RR8 start restores the validated SPI timing (`PSCL=8`, `MIDI=4`); `NSS_MIDI` and `SPI_PSCL` return `ERR busy` while a Framework stream is active or armed.

## Hardware

| Item | Value |
| --- | --- |
| MCU | STM32H743VIT6, Cortex-M7, LQFP100 |
| HSE | 8 MHz on PH0/PH1 |
| Core clock | 480 MHz, VOS0 by default |
| USB | USB OTG HS with external USB3300 ULPI PHY |
| USB interface | Vendor Bulk, VID:PID `0483:5741`, OUT `0x01`, IN `0x81` |
| Intan SPI2 | PA9 SCK, PB14 MISO, PC1 MOSI, about 25 MHz |
| Intan CS | PA11 / SPI2_NSS, hardware pulsed NSS |

`PE11` is retained only as a legacy software-CS/TIM1 option when `INTAN_CS_HW_NSS=OFF`.

Every RHS2116 32-bit word must be its own CS transaction. Do not combine words under one asserted CS, including stimulation patterns.

## Build

The default configuration builds without Intan hardware. For the target board use:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build
```

| Option | Default | Meaning |
| --- | --- | --- |
| `WITH_INTAN_HW` | `OFF` | Enable SPI2/RHS2116 bringup |
| `INTAN_CS_HW_NSS` | `ON` | Use PA11 SPI2_NSS; OFF selects legacy PE11 |
| `BOARD_HAS_LSE` | `OFF` | Enable 32.768 kHz LSE for RTC |
| `BOARD_SYSCLK_480` | `ON` | 480 MHz VOS0; OFF selects 240 MHz legacy mode |

Firmware image: `build/WeActSTM32H743.elf`.

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

## USB protocol

The binary stream uses `RHS1` frames of exactly 4096 bytes:

| Field | Size |
| --- | ---: |
| Header | 32 bytes |
| ADC response payload | 2032 x `uint16_t` |
| Frame ring | 32 frames in non-cacheable D2 SRAM |

The frame ABI is defined in [usb_stream_frame.h](Core/Inc/usb_stream_frame.h). `reserved` contains stream metadata: first channel, channel count, CONVERT flags, and channel-tag width.

The USB PCD intentionally runs without peripheral DMA. The producer does not wait for USB; a full ring increments `usb_overflow_count`. `STOP` is accepted in the hot loop, completes the current SPI DMA sequence at EOT, then aborts an active frame transfer before the ring buffer is reset. A 1 ms DWT deadline aborts a failed Framework DMA sequence; `STATS` reports the cumulative `fw_dma_err` count.

## Commands

Basic control:

```text
PING
STOP
STATS
SYNTH_STREAM <samples>
```

RHS2116 control:

```text
ID
READ <reg>
WRITE <reg> <value> [u] [m]
INIT_RECORD [adc_ksps]
INIT_STIM
CLEAR_ADC
CLEAR_COMP
CONVERT <channel> [flags]
IMPEDANCE_MEASURE <channel> <scale_bits> <freq_hz> <samples_per_period> <periods> <flags>
```

Diagnostic and legacy stream commands are implemented, but are not a substitute for `SPI_STREAM_FW ... 40` in production. Stimulation patterns must use the per-slot `Intan_Xfer32Word()` path; see [intan_stim_pattern_guide.md](intan_stim_pattern_guide.md).

## Validation

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_frame_bench.py -n 50000 --no-reset --runs 5
python3 tools/ch_fw_long_suite.py --duration 10 --ksps 40
```

For a production capture expect `sysclk_mhz=480`, `sck_khz=25000`, `sample_clip=0`, and `usb_ovf=0`. Check that USB enumerates at 480M with `lsusb -t`.

## Repository map

```text
Core/Src/main.c                 Boot, MPU, clocks, main loop
Core/Src/spi.c                  SPI2 and PA11 hardware NSS setup
Core/Src/intan_spi.c            RHS2116 protocol and DMA paths
Core/Src/intan_fw_acq.c         RR8 production acquisition
Core/Src/intan_stream.c         ADC samples to RHS1 frames
Core/Src/usb_stream_service.c   Commands, stream producer, statistics
Core/Src/usb_vendor_bulk.c      USB Vendor Bulk class
Core/Inc/usb_stream_frame.h     RHS1 ABI
tools/                          Host tools, validation, and capture scripts
```
