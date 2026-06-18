# Конспект Intan RHD/RHS STM32 Firmware Framework v1.2

Источник: Intan Technologies, **RHD/RHS STM32 Firmware Framework**, Version 1.2, 18 June 2025  
URL: https://intantech.com/files/Intan_RHD_RHS_STM32_Framework.pdf

**Оригинальный код (v1.2):** `Intan RHS2116/Intan_RHD_RHS_STM32_Framework_v1_2/H7/rhs_acquisition/`  
Карта портирования в наш проект: [intan_framework_port_map.md](./intan_framework_port_map.md)

Цель этого конспекта: выделить из документа практические вещи для проекта на **STM32H7 + RHS2116**, особенно для задачи:

```text
RHS2116 -> SPI DMA -> буферизация -> USB HS / ULPI -> внешнее устройство
```

---

# 1. Главная идея документа

Intan даёт open-source STM32 framework для работы с чипами:

- RHD2216
- RHD2132
- RHD2164
- RHS2116

Фреймворк рассчитан на потоковую регистрацию данных с Intan-чипов через STM32, с использованием:

- SPI
- таймеров
- прерываний
- DMA
- HAL или LL драйверов
- USART для демонстрационной передачи данных наружу

Для твоего проекта самое важное:

```text
В Intan framework уже есть H7 rhs_acquisition.
Это референс для STM32H7 + RHS2116 + SPI + DMA + sample timer.
```

Но транспорт наружу в примере сделан через USART. Для твоей платы его надо заменить на:

```text
USB HS через OTG_HS + внешний ULPI PHY
```

---

# 2. Почему STM32U5/H7, а не любой STM32

Документ отдельно объясняет, что STM32U5 и STM32H7 выбраны не случайно.

Ключевые причины:

- достаточная скорость CPU;
- подходящий SPI-блок;
- возможность автоматического NSS/CS pulse между словами;
- нормальная работа SPI вместе с DMA;
- таймеры для стабильного sample rate;
- возможность вывода данных через внешние интерфейсы.

Особенно важно:

```text
STM32H7/U5 проверены Intan на автоматическое поднятие NSS/CS между 16-bit или 32-bit словами.
```

Это критично для Intan, потому что чипу нужен явный `CS high` между словами.

---

# 3. SPI-требования Intan

## 3.1. CS/NSS pulse обязателен

В документе сказано, что для Intan SPI критично:

```text
CS/NSS должен подняться high между каждым SPI word.
```

Для RHD это 16-bit word.  
Для RHS это 32-bit word.

По документу Intan указывает:

```text
CS high между словами должен быть не меньше 154 ns.
```

Для RHS2116 это означает:

```text
один CS-low участок = один 32-bit SPI frame
между соседними 32-bit frame должен быть CS-high gap
```

Нельзя делать так:

```text
CS low
word0 word1 word2 word3 ...
CS high
```

Нужно так:

```text
CS low  -> 32-bit word0 -> CS high
CS low  -> 32-bit word1 -> CS high
CS low  -> 32-bit word2 -> CS high
...
```

---

## 3.2. Почему STM32F4 плох для Intan

Документ прямо говорит, что у STM32F4 SPI обычно держит NSS низким на весь burst и не пульсирует между словами так, как надо Intan.

Для F4 приходится отвязывать CS от SPI и дёргать его GPIO:

```text
CS low
SPI word
CS high
delay
CS low
SPI word
...
```

Это:

- тратит CPU;
- мешает использовать DMA для больших последовательностей;
- нормально только для низких sample rate, примерно до 5 kS/s;
- плохо масштабируется.

Для твоего проекта это важный вывод:

```text
GPIO CS через CPU - плохой путь для высокой скорости.
Если аппаратный NSS pulse на H7 работает правильно, его надо использовать.
```

---

# 4. RHS2116 отличается от RHD

## 4.1. RHS использует 32-bit SPI words

Главное отличие RHS от RHD:

```text
RHD: 16-bit SPI words
RHS: 32-bit SPI words
```

Следствия:

- RHS-регистры 32-битные;
- READ/WRITE команды 32-битные;
- CONVERT команды 32-битные;
- MISO response тоже 32-битный;
- внутри 32-битного response могут быть полезные 16-bit данные.

Не надо путать:

```text
полезный ADC response 16 bit
```

и

```text
физическая SPI транзакция RHS = 32 bit
```

Для SPI/DMA на STM32 это значит:

```text
SPI DataSize = 32 bit
DMA memory/peripheral alignment = word
Size в HAL_SPI_TransmitReceive_DMA = количество 32-bit frames
```

---

## 4.2. RHS sample period состоит из 20 команд

Для `rhs_acquisition` Intan использует один sample period, внутри которого отправляется:

```text
16 CONVERT commands + 4 auxiliary commands = 20 SPI words
```

Для RHS:

```text
20 commands * 32 bit = 640 bit на один sample period
```

При sample rate 20 kS/s:

```text
20 000 sample periods/s * 20 words = 400 000 SPI words/s
```

То есть даже при 20 kS/s нагрузка на SPI-тракт уже серьёзная. Делать это CPU-петлёй нельзя.

---

# 5. Общая структура программы Intan

Документ описывает одинаковую архитектуру для всех проектов.

## 5.1. Инициализация

Последовательность примерно такая:

```text
1. STM32CubeIDE генерирует init периферии из .ioc.
2. Код настраивает параметры RHD/RHS регистров.
3. Регистры записываются в Intan-чип через WRITE commands.
4. Создаётся command_sequence_MOSI.
5. Включается зелёный LED.
6. Включается sample timer.
7. Программа входит в acquisition loop.
```

Для RHS:

```text
command_sequence_MOSI = 16 CONVERT + 4 AUX commands
```

---

## 5.2. Таймер sample rate

Sample rate задаётся таймером:

```text
INTERRUPT_TIM
```

В примерах таймер настроен на:

```text
20 kHz
```

Каждый interrupt timer вызывает:

```text
sample_processing_routine()
```

Это одна из самых важных функций архитектуры.

---

## 5.3. sample_processing_routine()

Эта функция вызывается один раз на sample period.

Она:

1. проверяет, завершилась ли предыдущая SPI sequence;
2. запускает новую SPI DMA sequence;
3. поднимает/опускает monitor pins для диагностики;
4. выполняет часть error checking.

Важное: функция **только стартует SPI DMA**, а не ждёт её окончания.

Правильная логика:

```text
Timer interrupt:
    старт SPI DMA sequence
    быстро выйти из interrupt
```

Плохая логика:

```text
Timer interrupt:
    отправить все 20 слов вручную
    обработать данные
    отправить наружу
    выйти
```

Это убьёт скорость и даст битые транзакции.

---

## 5.4. SPI DMA complete callback

Когда SPI sequence закончилась, срабатывает callback:

```text
spi_rx_cplt_callback()
или
spi_txrx_cplt_callback()
```

После этого код:

- обрабатывает MISO результаты;
- записывает данные в память, если включён offline mode;
- или запускает real-time transmission, если offline mode выключен;
- обновляет `command_transfer_state`.

Состояния:

```text
TRANSFER_WAIT
TRANSFER_COMPLETE
TRANSFER_ERROR
```

---

# 6. Важные ошибки из документа

## 6.1. SampleClip

`SampleClip` возникает, если следующий sample period наступает раньше, чем завершилась предыдущая SPI sequence.

Причина:

```text
sample period слишком короткий
или SPI sequence слишком длинная
или команд слишком много
или SPI baud rate слишком низкий
```

Лечение:

- снизить sample rate;
- уменьшить количество CONVERT commands;
- уменьшить количество AUX commands;
- ускорить SPI, но не выше лимита Intan;
- убрать тяжёлую обработку из interrupt/callback;
- перейти с HAL на LL, если CPU overhead реально мешает.

---

## 6.2. InterruptClip

`InterruptClip` возникает, если обработка внутри interrupt/callback занимает слишком долго и залезает в следующий sample period.

Частые причины:

- blocking transmit;
- медленный USART/USB transmit внутри interrupt;
- сложная обработка данных в callback;
- лишние циклы копирования;
- форматирование данных через printf/sprintf;
- отправка слишком мелкими пакетами.

Для твоего проекта это особенно важно:

```text
USB/ULPI передачу нельзя делать как "получил sample -> сразу отправил маленький кусок".
Нужно складывать данные в блоки и отправлять крупными USB transfers.
```

---

## 6.3. InvalidComplianceReading

Для RHS есть compliance monitor. Документ описывает проверку результата READ compliance monitor.

Если ожидаемый READ result не найден, код может уйти в ошибку:

```text
InvalidComplianceReading
```

Возможные причины:

- SPI physical/protocol problem;
- Intan-чип отсутствует;
- результат READ не найден из-за двухкомандной pipeline delay;
- нарушена логика AUX slots;
- изменили количество AUX commands, но не поправили compliance parsing.

Если ты меняешь AUX-команды RHS, надо внимательно проверить:

```text
rhsinterface.c
locate_compliance_result()
process_compliance_data()
spi_txrx_cplt_callback()
```

---

# 7. Двухкомандная задержка Intan pipeline

Документ подчёркивает важную особенность:

```text
MISO response на MOSI command приходит через 2 команды.
```

То есть если ты отправил CONVERT для канала N, результат не лежит в том же индексе MISO sequence. Код Intan учитывает это через `+2 offset`.

Для твоего парсера это критично.

Нельзя просто делать:

```c
result[channel] = miso[channel];
```

Нужно учитывать pipeline:

```c
result[channel] = miso[channel + 2];
```

А для AUX slots часть результатов может попадать уже в следующую sequence.

---

# 8. Пользовательские файлы, которые Intan предлагает менять

Документ называет три основных файла для пользовательских изменений:

```text
userconfig.h
userfunctions.h
userfunctions.c
```

А вот `.ioc` файл надо менять через STM32CubeIDE, если нужно изменить:

- пины;
- SPI baud rate;
- timer sample rate;
- DMA;
- USART;
- периферийные настройки.

---

# 9. userconfig.h: важные define

## 9.1. USE_HAL

```c
#define USE_HAL
```

Если включено - код собирается под HAL.

Если закомментировано - код собирается под LL.

Вывод:

```text
HAL проще для старта.
LL быстрее и ближе к регистрам.
```

Для твоей проблемы со скоростью:

```text
начать можно с HAL,
но высокоскоростной production path лучше делать через LL/регистры там, где HAL создаёт лишний overhead.
```

---

## 9.2. OFFLINE_TRANSFER

```c
#define OFFLINE_TRANSFER
```

Если включено:

```text
данные пишутся в RAM во время acquisition,
а наружу передаются после завершения acquisition.
```

Если выключено:

```text
данные передаются наружу в реальном времени.
```

Для твоей задачи USB/ULPI real-time stream:

```text
OFFLINE_TRANSFER должен быть выключен,
но вместо USART realtime нужно сделать USB bulk realtime.
```

Однако слепо передавать каждый sample period нельзя. Нужна блочная буферизация.

---

## 9.3. SAMPLE_DC_AMPS

RHS-only.

Если включено:

```text
в CONVERT response вместе с AC amplifiers читаются DC amplifiers.
```

Поскольку RHS MISO 32-битный, AC и DC могут находиться в одном 32-bit response.

Если тебе нужны только 16-bit AC данные, можно выключить DC sampling, если это соответствует твоему режиму.

---

## 9.4. AUTO_STIM_CMD_MODE

RHS-only.

Если включено:

```text
4 AUX slots используются для WRITE-команд стимуляции в реальном времени.
```

Если выключено:

```text
AUX slots используются как обычные auxiliary command lists.
```

Для чистой регистрации без стимуляции:

```text
AUTO_STIM_CMD_MODE можно отключить,
а AUX_COMMANDS_PER_SEQUENCE можно уменьшить, если эти команды не нужны.
```

Но если используешь compliance monitor или стимуляцию, нельзя просто убрать AUX slots без изменения логики.

---

## 9.5. CONVERT_COMMANDS_PER_SEQUENCE

Для RHS default:

```c
#define CONVERT_COMMANDS_PER_SEQUENCE 16
```

Это количество каналов RHS2116, которые будут CONVERT в каждом sample period.

Если нужна скорость выше, можно уменьшить число CONVERT-команд.

Пример:

```text
16 каналов -> 16 CONVERT per sample period
8 каналов  -> 8 CONVERT per sample period
4 канала   -> 4 CONVERT per sample period
```

Это прямо уменьшает длину SPI sequence.

---

## 9.6. AUX_COMMANDS_PER_SEQUENCE

Для RHS default:

```c
#define AUX_COMMANDS_PER_SEQUENCE 4
```

Итого default RHS sequence:

```text
16 CONVERT + 4 AUX = 20 commands
```

Если AUX-команды не нужны, это место для ускорения.

Но осторожно:

```text
если AUTO_STIM_CMD_MODE включён,
stimscheduler.c ожидает 4 AUX slots.
```

Также надо проверить:

```text
rhsinterface.c
locate_compliance_result()
compliance parsing
pipeline delay
```

---

## 9.7. FIRST_SAMPLED_CHANNEL и NUM_SAMPLED_CHANNELS

Они управляют тем, какие каналы сохраняются/передаются наружу.

Для примера:

```c
#define FIRST_SAMPLED_CHANNEL 8
#define NUM_SAMPLED_CHANNELS 4
```

Это означает передачу каналов:

```text
8, 9, 10, 11
```

Важно различать:

```text
CONVERT_COMMANDS_PER_SEQUENCE = какие каналы физически запрашиваются у RHS
NUM_SAMPLED_CHANNELS = какие результаты сохраняются/передаются
```

Если физически конвертируешь 16 каналов, но наружу отправляешь 4, SPI всё равно тратит время на 16 CONVERT.

---

# 10. userfunctions.c: что менять под твой проект

## 10.1. write_data_to_memory()

В Intan-коде эта функция пишет данные в `sample_memory` в offline mode.

Для твоего проекта можно переделать её в:

```text
SPI MISO results -> acquisition block buffer
```

Но лучше не копировать по одному sample в тяжелую структуру внутри interrupt. Правильнее:

```text
callback помечает SPI buffer ready
main loop / low priority task упаковывает данные
```

---

## 10.2. transmit_data_realtime()

В Intan-коде эта функция раз в sample period запускает USART DMA для нескольких каналов.

Для твоего проекта это ключевая точка замены:

```text
старое:
    transmit_data_realtime() -> USART DMA

новое:
    transmit_data_realtime() -> положить данные в USB block/ring buffer
```

Не надо делать USB transmit прямо на каждый sample period.

Правильная модель:

```text
накапливать 256 или 512 sample periods
затем отправлять один крупный USB HS bulk transfer
```

---

## 10.3. transmit_dma_to_usart()

Intan использует эту функцию как неблокирующий запуск USART DMA.

В твоём проекте аналог должен быть:

```text
transmit_block_to_usb()
```

Логика:

```c
int transmit_block_to_usb(uint8_t *buf, uint32_t len)
{
    if (!usb_ready) {
        return USB_BUSY;
    }

    usb_ready = false;
    start_usb_bulk_in_transfer(buf, len);
    return USB_OK;
}
```

А освобождать буфер нужно только в USB TX complete callback.

---

## 10.4. configure_convert_commands()

Эта функция задаёт порядок CONVERT-команд.

Если тебе нужны все 16 каналов:

```text
оставить порядок 0..15
```

Если нужны только некоторые каналы:

```text
уменьшить CONVERT_COMMANDS_PER_SEQUENCE
и передать свой массив channel_numbers
```

Это один из самых сильных способов поднять sample rate.

---

## 10.5. configure_aux_commands()

Эта функция задаёт AUX command slots.

Если ты не используешь:

- стимуляцию;
- compliance monitor;
- периодическое переписывание регистров;
- impedance check;

то AUX можно сокращать.

Но нельзя делать это механически. Надо проверить зависимости:

```text
AUTO_STIM_CMD_MODE
stimscheduler.c
rhsinterface.c
locate_compliance_result()
two-command pipeline delay
```

---

# 11. Что взять из Intan architecture для USB/ULPI

Документ использует USART, но сама архитектура переносится на USB.

## 11.1. Не отправлять по одному response

Плохо:

```text
получил 16 bit response -> отправил 2 байта по USB
получил 16 bit response -> отправил 2 байта по USB
...
```

Это почти гарантированно даст низкую скорость.

Правильно:

```text
SPI DMA sequence готова
данные сложены в block buffer
когда блок накоплен - USB bulk transfer
```

Для USB HS bulk endpoint удобно делать размеры, кратные 512 байтам.

---

## 11.2. Рекомендуемый блок для RHS2116

Если передаёшь только 16 каналов AC по 16 бит:

```text
16 channels * 2 bytes = 32 bytes на sample period
```

Для 256 sample periods:

```text
32 * 256 = 8192 bytes
```

Это хороший размер:

```text
8192 bytes = 16 * 512-byte USB HS packets
```

Если передаёшь 20 responses на sample period:

```text
20 responses * 2 bytes = 40 bytes
40 * 256 = 10240 bytes
```

Тоже хороший размер:

```text
10240 bytes = 20 * 512-byte USB HS packets
```

---

## 11.3. Минимальная структура USB-блока

Рекомендуемый формат:

```c
typedef struct __attribute__((packed, aligned(32))) {
    uint32_t magic;
    uint32_t block_seq;
    uint32_t first_sample_index;
    uint16_t sample_count;
    uint16_t channel_count;
    uint32_t flags;
    uint32_t spi_error_count;
    uint32_t usb_busy_count;
    uint32_t dropped_block_count;
    uint16_t data[256][16];
    uint32_t crc32;
} rhs_usb_block_t;
```

Зачем это нужно:

- `magic` - быстро ловит смещение/битые блоки;
- `block_seq` - ловит потери USB-блоков;
- `first_sample_index` - ловит пропуски sample periods;
- `flags` - можно записывать SampleClip/InterruptClip/USB overflow;
- `crc32` - отличает транспортную порчу от ошибки парсинга.

Без этих полей ты будешь гадать, где именно ломается поток.

---

# 12. Рекомендуемый pipeline для твоей платы

## 12.1. Правильная схема

```text
TIM3 sample interrupt
    |
    v
start SPI DMA sequence:
    20 x 32-bit RHS frames
    |
    v
SPI DMA complete callback
    |
    v
parse/mark MISO buffer ready
    |
    v
ring buffer / block accumulator
    |
    v
USB HS bulk IN transfer via OTG_HS + ULPI PHY
    |
    v
external device
```

---

## 12.2. Буферизация

Минимум:

```text
double buffer
```

Лучше:

```text
4-8 block ring buffer
```

Состояния блока:

```text
FREE
FILLING
READY_FOR_USB
SENDING_USB
```

Правило:

```text
USB не должен читать блок, который SPI/CPU ещё пишет.
CPU не должен перезаписывать блок, который USB ещё отправляет.
```

---

## 12.3. Где выполнять работу

В timer interrupt:

```text
минимум работы
только старт SPI DMA
```

В SPI complete callback:

```text
минимум работы
пометить sequence готовой
инкрементировать счётчики
```

В main loop / low priority task:

```text
парсинг
упаковка в USB block
запуск USB transfer, если endpoint свободен
```

В USB TX complete callback:

```text
пометить block FREE
запустить следующий READY block, если есть
```

---

# 13. Проверки на осциллографе / логическом анализаторе

Для RHS2116 + H7 проверить обязательно:

```text
1. На один CS-low участок ровно 32 SCK.
2. CS high между 32-bit frames >= 154 ns по документу framework.
3. SCK не выше 25 MHz.
4. SPI sequence содержит ожидаемое число frames: обычно 20.
5. На один sample period приходится одна sequence.
6. Следующая sequence не начинается до завершения предыдущей.
7. Нет лишних пауз внутри 32-bit frame.
8. Нет сдвоенных/пропущенных CS pulses.
```

Если hardware NSS pulse не делает то, что нужно, не надо возвращаться к CPU GPIO на высокой скорости. Лучше:

```text
TIM-generated CS + SPI DMA
```

Но сначала проверь H7 hardware NSS pulse, потому что именно его Intan считает рабочим для U5/H7.

---

# 14. Почему у тебя могут быть "битые транзакции"

Если SPI-протокол сам по себе правильный, то вероятные причины:

## 14.1. Нарушение ownership буфера

Пример плохой ситуации:

```text
SPI DMA пишет buffer A
USB уже отправляет buffer A
CPU параллельно переписывает buffer A
```

Результат:

```text
часть блока старая
часть блока новая
часть блока мусор
```

Лечение:

```text
ring buffer + состояния блоков + запрет перезаписи до USB complete
```

---

## 14.2. D-cache на STM32H7

Для STM32H7 это классическая причина битых данных при DMA.

Правила:

```text
DMA buffers aligned(32)
размеры кратны 32 байтам
перед DMA TX: CleanDCache
после DMA RX: InvalidateDCache
не класть DMA buffers в недоступную DMA память
лучше выделить non-cacheable область MPU под DMA buffers
```

Если этого нет - данные могут быть "битые", хотя SPI и USB физически работают.

---

## 14.3. Слишком мелкие USB transfers

USB HS не любит поток из крошечных передач.

Плохо:

```text
2 bytes
4 bytes
32 bytes
40 bytes
```

Хорошо:

```text
512 bytes
1024 bytes
4096 bytes
8192 bytes
10240 bytes
16384 bytes
```

Для RHS2116 удобно:

```text
8192 bytes для 256 samples * 16 channels * 2 bytes
```

---

## 14.4. Игнорирование USB busy

Если USB stack вернул busy, нельзя считать, что данные отправлены.

Правильно:

```text
если USB busy -> блок остаётся READY_FOR_USB
если USB complete -> блок FREE
```

Неправильно:

```text
если USB busy -> перезаписать буфер следующими данными
```

---

## 14.5. Blocking USB/USART внутри sample path

Документ прямо предупреждает: blocking transmit может занять слишком долго и вызвать timing error.

Для твоего проекта:

```text
никаких blocking transfer внутри timer interrupt или SPI callback
никакого printf/sprintf в sample path
никакой отправки по USB в цикле ожидания
```

---

# 15. Как ускоряться по документу

## 15.1. Перейти HAL -> LL

Документ рекомендует стартовать с HAL, но для производительности смотреть LL.

Для твоего проекта это разумный порядок:

```text
1. Сначала добиться правильной архитектуры на HAL.
2. Потом заменить hot path на LL/регистры.
```

Hot path:

```text
sample timer
SPI DMA start
SPI callback
USB block queue
USB TX complete
```

---

## 15.2. Уменьшить количество SPI commands

Самый прямой способ ускорения:

```text
уменьшить CONVERT_COMMANDS_PER_SEQUENCE
уменьшить AUX_COMMANDS_PER_SEQUENCE
```

Для RHS default:

```text
16 + 4 = 20 commands
```

Если нужно только 8 каналов и без AUX:

```text
8 + 0 = 8 commands
```

Это в 2.5 раза короче по SPI sequence.

Но AUX нельзя убирать, если они нужны для:

- стимуляции;
- compliance monitor;
- register refresh;
- impedance check;
- служебной диагностики.

---

## 15.3. Поднять SPI baud rate до разумного максимума

Документ говорит, что примеры обычно работают около:

```text
20-24 Mbit/s
```

Лимит Intan:

```text
25 MHz SCLK
```

Для STM32H7 можно целиться в:

```text
SCK около 24-25 MHz
```

Но обязательно проверить:

```text
CS high >= 154 ns
MIDI/MSSI выставлены корректно
нет нарушения Intan timing
```

---

## 15.4. Убрать лишнюю обработку из sample period

В sample path нельзя делать:

- тяжёлый парсинг;
- форматирование;
- копирование больших буферов;
- USB transmit с ожиданием;
- debug print;
- динамическое выделение памяти;
- CRC на каждый sample period, если он тяжёлый.

CRC лучше считать на готовый USB block, вне interrupt.

---

# 16. Что заменить в Intan framework под USB HS / ULPI

## Было у Intan

```text
SPI DMA acquisition
    -> transmit_data_realtime()
    -> transmit_dma_to_usart()
    -> USART DMA
```

## Должно быть у тебя

```text
SPI DMA acquisition
    -> push_sample_to_usb_block()
    -> usb_block_queue
    -> USB HS bulk IN
```

---

## Минимальный план замены

### Шаг 1

Взять проект:

```text
H7 rhs_acquisition
```

### Шаг 2

Оставить почти без изменений:

```text
configure_registers()
configure_convert_commands()
configure_aux_commands()
sample_processing_routine()
SPI DMA start
SPI complete callback
command_transfer_state
error handling
```

### Шаг 3

Переделать:

```text
transmit_data_realtime()
transmit_dma_to_usart()
USART callbacks
```

в:

```text
push_rhs_samples_to_block()
usb_try_start_next_transfer()
USB TX complete callback
```

### Шаг 4

Добавить:

```text
rhs_usb_block_t blocks[4 или 8]
block state machine
block_seq
sample_counter
dropped_block_count
usb_busy_count
crc32
```

### Шаг 5

На стороне USB:

```text
USB HS Device
Vendor class или bulk endpoint
Bulk IN endpoint max packet size = 512
крупные transfers кратные 512
```

---

# 17. Приоритеты interrupt/DMA

Рекомендуемый смысл приоритетов:

```text
1. Sample timer / SPI DMA complete - высоко
2. USB HS interrupt - ниже или аккуратно сопоставимо
3. main loop processing - низко
4. debug UART / printf - убрать из hot path
```

Главное правило:

```text
регистрация данных важнее отправки данных наружу.
```

Если USB не успевает, надо:

```text
считать dropped blocks
помечать overflow
но не ломать SPI acquisition timing
```

---

# 18. Конкретный checklist для твоей платы

## SPI/RHS

- [ ] SPI DataSize = 32 bit.
- [ ] DMA TX alignment = word.
- [ ] DMA RX alignment = word.
- [ ] Hardware NSS pulse проверен логическим анализатором.
- [ ] CS high между 32-bit frames >= 154 ns.
- [ ] SCK <= 25 MHz.
- [ ] Sequence length ожидаемая: 20 frames default или меньше, если ты сократил команды.
- [ ] Учтён two-command pipeline delay.
- [ ] AUX slots не сломали compliance/stim logic.
- [ ] `SampleClip` счётчик/индикатор есть.

## Буферы

- [ ] SPI RX/TX buffers aligned(32).
- [ ] USB TX blocks aligned(32).
- [ ] DMA buffers не лежат в неподходящей памяти.
- [ ] D-cache clean/invalidate сделаны правильно или область non-cacheable.
- [ ] Есть ring buffer минимум на 4 блока.
- [ ] Есть состояния блоков.
- [ ] Нет перезаписи блока до USB TX complete.

## USB HS / ULPI

- [ ] Используется именно USB HS через ULPI PHY, не FS.
- [ ] Bulk IN endpoint max packet = 512 bytes.
- [ ] Передачи крупными блоками: 8192/10240/16384 bytes.
- [ ] USB busy не игнорируется.
- [ ] TX complete callback освобождает блок.
- [ ] Нет blocking USB transmit в sample path.

## Диагностика

- [ ] В USB-блоке есть `magic`.
- [ ] Есть `block_seq`.
- [ ] Есть `first_sample_index`.
- [ ] Есть `dropped_block_count`.
- [ ] Есть `spi_error_count`.
- [ ] Есть `usb_busy_count`.
- [ ] Есть CRC или checksum.
- [ ] Есть GPIO monitor pins: sample interrupt, SPI busy, main idle/free time.
- [ ] Логический анализатор проверил CS/SCK/MOSI/MISO.

---

# 19. Жёсткий вывод

По документу Intan правильная архитектура для RHS2116 на STM32H7 такая:

```text
sample timer
    -> SPI DMA sequence из 20 x 32-bit words
    -> hardware NSS pulse между словами
    -> callback по завершению sequence
    -> минимальная обработка
    -> неблокирующая передача наружу
```

Для твоего проекта неверно пытаться делать:

```text
RHS response -> сразу маленькая USB/ULPI отправка
```

Правильный вариант:

```text
RHS response -> block buffer -> USB HS bulk transfer
```

Если сейчас скорость мала и часть транзакций битая, самые вероятные проблемы не в SPI-протоколе, а в:

```text
1. слишком мелких передачах наружу;
2. отсутствии ring buffer;
3. перезаписи буфера до завершения USB transfer;
4. D-cache/DMA coherency на STM32H7;
5. blocking transmit в interrupt/callback;
6. игнорировании USB busy;
7. неправильной обработке двухкомандной задержки MISO result;
8. SampleClip/InterruptClip, которые не диагностируются явно.
```

Начальная точка для кода:

```text
Intan H7 rhs_acquisition
```

Менять в первую очередь:

```text
transmit_data_realtime()
transmit_dma_to_usart()
USART-related callbacks
```

на:

```text
USB HS bulk block queue
```

А SPI acquisition path лучше сначала не ломать: он уже сделан Intan именно под RHS2116, 32-bit SPI words, NSS pulse и DMA.

