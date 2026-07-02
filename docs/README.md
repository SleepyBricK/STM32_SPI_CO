# Документация WeActSTM32H743

Этот каталог содержит полную техническую документацию прошивки для пользовательской платы на STM32H743VIT6 с Intan RHS2116 по SPI2 и USB HS Vendor Bulk. Прошивка передаёт поток ADC-ответов фиксированными фреймами `RHS1` по интерфейсу `0483:5741` и принимает текстовые команды по тому же bulk-интерфейсу.

Главный рабочий режим проекта - `SPI_STREAM_FW n 255 0 40`: RR8, каналы `0..7`, 40 kS/s на канал. Этот режим использует DWT phase-paced hot loop, аппаратный CS на `PA11/SPI2_NSS`, SPI2 около 25 MHz и USB HS bulk IN/OUT. Остальные режимы сохранены как diagnostic/legacy и не заменяют production acquisition.

Источник правды по ограничениям платы и правилам изменений: [AGENTS.md](../AGENTS.md) и [README.md](../README.md). Документы ниже синтезируют эти сведения с текущим кодом `Core/Src`, `Core/Inc` и `tools`.

## Разделы

| Документ | Содержание |
| --- | --- |
| [01_overview.md](01_overview.md) | Назначение, характеристики, валидированный режим, ограничения |
| [02_hardware.md](02_hardware.md) | MCU, тактирование, пины, Intan, CS-инвариант, CMake и прошивка |
| [03_architecture.md](03_architecture.md) | Архитектура firmware, main loop, SPI/DMA, ring, USB pump, STOP, fault/IWDG |
| [04_usb_protocol.md](04_usb_protocol.md) | Vendor Bulk, endpoints, `RHS1` ABI, metadata, overflow semantics |
| [05_commands.md](05_commands.md) | Полный справочник текстовых USB-команд из `usb_commands.c` |
| [06_acquisition.md](06_acquisition.md) | Production `SPI_STREAM_FW`, RR8, pacing, PSCL/MIDI, STOP и счётчики |
| [07_host_tools.md](07_host_tools.md) | Python tools, захват, валидация, ожидаемые `STATS` |
| [08_development.md](08_development.md) | Структура репозитория, сборка, отладка, правила изменений |
| [09_troubleshooting.md](09_troubleshooting.md) | Типичные проблемы: USB, clipping, DMA, CS, disconnect |

## Специализированные гайды

| Документ | Назначение |
| --- | --- |
| [intan_stim_pattern_guide.md](../intan_stim_pattern_guide.md) | Стимуляционные паттерны RHS2116 и безопасная послотовая отправка |
| [intan_impedance_guide.md](../intan_impedance_guide.md) | Измерение импеданса через `IMPEDANCE_MEASURE` |
| [intan_pattern_testing_guide.md](../intan_pattern_testing_guide.md) | Проверка паттернов и стендовые сценарии |
| [docs/conference_report_ru.md](conference_report_ru.md) | Популярный отчёт для конференции |

`usb_hs_streaming_v2_clean_slate_guide.md` в корне считается устаревшим по части CS-пина: он описывает PE11 как CS. Для текущей платы primary CS - `PA11/SPI2_NSS`.
