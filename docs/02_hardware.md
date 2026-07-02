# Аппаратная часть

## MCU и тактирование

Целевая плата использует STM32H743VIT6 с внешним HSE 8 MHz. По умолчанию проект собирается для SYSCLK 480 MHz (`BOARD_SYSCLK_480=ON`): PLL1 даёт 480 MHz, AHB делится до 240 MHz, APB домены работают с делителями `/2`.

SPI2 kernel clock фиксирован от PLL2P = 200 MHz в обоих режимах SYSCLK. При SPI prescaler `/8` получается SCK около 25 MHz, что является валидированной рабочей точкой для RHS2116.

| Параметр | 480 MHz mode | Legacy 240 MHz mode |
| --- | ---: | ---: |
| CMake option | `BOARD_SYSCLK_480=ON` | `BOARD_SYSCLK_480=OFF` |
| Voltage scale | VOS0 | VSCALE2 |
| SYSCLK | 480 MHz | 240 MHz |
| AHB | 240 MHz | 120 MHz |
| SPI2 kernel | PLL2P 200 MHz | PLL2P 200 MHz |

## Пины

| Функция | Пин | Режим | Комментарий |
| --- | --- | --- | --- |
| HSE OSC_IN/OUT | PH0/PH1 | clock | 8 MHz |
| LSE | PC14/PC15 | clock | Только при `BOARD_HAS_LSE=ON` |
| SPI2 SCK | PA9 | AF5 SPI2 | RHS2116 SCK |
| SPI2 MISO | PB14 | AF5 SPI2 | RHS2116 MISO |
| SPI2 MOSI | PC1 | AF5 SPI2 | RHS2116 MOSI |
| Intan CS primary | PA11 | AF5 SPI2_NSS | Hardware pulsed NSS, active low |
| Intan CS legacy | PE11 | GPIO/TIM1 path | Только при `INTAN_CS_HW_NSS=OFF` |
| UART1 TX/RX | PB6/PB7 | USART1 | 115200 8N1; CLI TX отключён в normal path |
| Fault blink | PB6 | GPIO blink | SOS/группы вспышек при fault |
| USB HS | ULPI pins | AF USB_OTG_HS | External USB3300 PHY |

`MX_SPI2_Init()` вызывается только при `INTAN_HW_PRESENT=1`, то есть при сборке `WITH_INTAN_HW=ON`.

## SPI2 и RHS2116

SPI2 настроен как master, 2-line, 32-bit data size, CPOL low, CPHA 1 edge, MSB first. Для `INTAN_CS_HW_NSS=ON` используется:

| Настройка | Значение |
| --- | --- |
| `NSS` | `SPI_NSS_HARD_OUTPUT` |
| `NSSPMode` | `SPI_NSS_PULSE_ENABLE` |
| `MasterInterDataIdleness` | runtime MIDI, production `4` SCK cycles |
| `BaudRatePrescaler` | `/8` |
| `NSSPolarity` | low-active |

RHS2116 требует паузу CS high между 32-битными словами. При hardware NSS это обеспечивают NSSP и MIDI. При legacy software-CS путь обязан поднимать GPIO CS между словами.

## Инвариант CS

Обязательное правило для всех путей:

```text
один 32-bit RHS2116 word == одна CS-транзакция
CS low -> 32-bit transfer -> CS high
```

Следствия:

| Операция | CS-транзакции |
| --- | ---: |
| `WRITE` register | 3 |
| `READ` register | 3 |
| `CONVERT` | 3 |
| `PATTERN_ADD_RAW` slot | 1 |
| `PATTERN_ADD_WRITE/READ/CONVERT` | 3 |
| `PATTERN_RUN` | Послотово через `Intan_Xfer32Word()` |

Нельзя объединять stim-команды, `READ`, `WRITE` или `CONVERT` в один длинный transfer под удержанным CS.

## USB HS

USB работает через OTG HS и внешний USB3300 ULPI PHY. Vendor Bulk class имеет один интерфейс с двумя bulk endpoints:

| Endpoint | Направление | Назначение |
| --- | --- | --- |
| `0x01` | OUT | Текстовые команды |
| `0x81` | IN | Текстовые ответы и бинарные `RHS1` frames |

В `USBD_LL_Init()` peripheral DMA отключён: `hpcd_USB_OTG_HS.Init.dma_enable = DISABLE`. RX FIFO и TX FIFO настроены вручную; frame buffers размещены в D2 SRAM и MPU-помечены non-cacheable.

## CMake options

| Опция | Default | Назначение |
| --- | --- | --- |
| `WITH_INTAN_HW` | `OFF` | Включает SPI2/RHS2116 bringup и hardware-команды |
| `INTAN_CS_HW_NSS` | `ON` | `PA11/SPI2_NSS`; `OFF` включает legacy PE11 |
| `BOARD_HAS_LSE` | `OFF` | RTC от 32.768 kHz LSE |
| `BOARD_SYSCLK_480` | `ON` | 480 MHz VOS0; `OFF` - 240 MHz legacy |

Сборка целевой платы:

```bash
cmake -S . -B build -DCMAKE_TOOLCHAIN_FILE=cmake/gcc-arm-none-eabi.cmake \
  -DWITH_INTAN_HW=ON -DINTAN_CS_HW_NSS=ON -DBOARD_SYSCLK_480=ON
cmake --build build
```

Прошивка:

```bash
STM32_Programmer_CLI -c port=SWD freq=400 ap=0 reset=HWrst \
  -w build/WeActSTM32H743.elf -v -rst
```

См. также [08_development.md](08_development.md).
