# Разработка

## Структура репозитория

| Путь | Роль |
| --- | --- |
| `Core/Src/main.c` | Boot, MPU, clocks, main loop |
| `Core/Src/spi.c` | SPI2 и `PA11/SPI2_NSS` hardware NSS setup |
| `Core/Src/intan_spi.c` | RHS2116 protocol, CS-инвариант, SPI/DMA paths |
| `Core/Src/intan_spi4_hw.c` | Low-level H7 SPI v4 access |
| `Core/Src/intan_fw_acq.c` | Production RR8 Framework acquisition |
| `Core/Src/intan_stream.c` | Упаковка ADC/counter samples в `RHS1` |
| `Core/Src/usb_stream_ring.c` | Frame ring в D2 SRAM |
| `Core/Src/usb_stream_service.c` | Command dispatch, producer, TX pump, `STATS` |
| `Core/Src/usb_commands.c` | Text command parser |
| `Core/Src/usb_vendor_bulk.c` | Vendor Bulk class и endpoint state |
| `Core/Inc/usb_stream_frame.h` | `RHS1` ABI |
| `tools/` | Host commands, validation, capture scripts |
| `docs/` | Русская документация проекта |

## Сборка

Целевая сборка:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build
```

`CMakeLists.txt` также прошивает build metadata в firmware:

| Define | Значение |
| --- | --- |
| `BUILD_TYPE` | `CMAKE_BUILD_TYPE` |
| `BUILD_GIT_HASH` | `git rev-parse --short=12 HEAD` или `unknown` |
| `INTAN_HW_PRESENT` | Из `WITH_INTAN_HW` |
| `INTAN_CS_HW_NSS` | Из одноимённой опции |
| `BOARD_HAS_LSE` | Из одноимённой опции |
| `BOARD_SYSCLK_480` | Из одноимённой опции |

## Прошивка

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

После прошивки:

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
```

## Правила изменений

| Область | Правило |
| --- | --- |
| STM32Cube blocks | Не трогать `USER CODE` blocks без необходимости |
| Пины/CS | Менять согласованно `intan_spi.h`, `spi.c`, `.ioc`, CMake и docs |
| `RHS1` ABI | Не менять без синхронного обновления host tools |
| USB/SPI/acquisition/clocks | Обновлять `README.md`, `AGENTS.md` и relevant docs |
| Production path | Не заменять `SPI_STREAM_FW n 255 0 40` diagnostic режимами |
| CS invariant | Сохранять одну CS-транзакцию на один 32-bit word |

## Отладка

Начинайте с `STATS`: clock fields, `sample_clip`, `usb_ovf`, `fw_dma_err`, `fw_late_seq`, `usb_disconnect`, `iwdg_reset`, `last_fault`.

Для USB проверяйте enumeration:

```bash
lsusb -t
```

Для SPI timing используйте `STATS`, `SPI_RATE*`, logic analyzer tools или Moku tools. При анализе CS помните, что primary CS - `PA11/SPI2_NSS`, а PE11 существует только для legacy `INTAN_CS_HW_NSS=OFF`.

## Документация

При изменении команд обновляйте [05_commands.md](05_commands.md). При изменении `UsbStreamFrame` обновляйте [04_usb_protocol.md](04_usb_protocol.md) и `tools/usb_intan_lib.py`. При изменении production acquisition обновляйте [06_acquisition.md](06_acquisition.md), [01_overview.md](01_overview.md) и root README.
