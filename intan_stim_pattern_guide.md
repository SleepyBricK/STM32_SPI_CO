# Гайд по формированию stim-паттернов RHS2116

Этот документ описывает рабочий способ формировать стимуляционные паттерны для Intan RHS2116 через USB text-команды прошивки STM32.

Ключевой инвариант: каждое 32-битное слово RHS2116 должно идти отдельной CS-транзакцией:

```text
CS low -> 32-bit word -> CS high
```

В текущей прошивке это обеспечивает `PATTERN_RUN` через `Intan_Xfer32Word()`. Не используйте grouped/DMA transfer для generic stimulation pattern: на практике он ломал видимый стим-сигнал.

## Базовые команды

```text
INIT_STIM
CLEAR_COMP
WRITE <reg> <value> <u> <m>
PATTERN_CLEAR
PATTERN_ADD_RAW <word>
PATTERN_ADD_DELAY_US <us>
PATTERN_STATUS
PATTERN_RUN <repeat>
READ 40
READ 42
READ 50
```

## Важные регистры

```text
R32 = 0xAAAA   ; unlock/enable stim block, делает INIT_STIM
R33 = 0x00FF   ; unlock/enable stim block, делает INIT_STIM
R42            ; triggered stim on/off mask
R44            ; triggered polarity mask
R64..R79       ; negative current magnitude per channel
R96..R111      ; positive current magnitude per channel
R40            ; compliance monitor latch
R50            ; realtime fault current detect
```

Для включения стимуляции также нужен аппаратный `stim_en = HIGH`.

## Ток стимуляции

Для канала `ch`:

```text
negative magnitude register = 64 + ch
positive magnitude register = 96 + ch
```

Значение тока кодируется как:

```text
0x8000 | current_uA
```

Пример для `180 мкА` на канале 0:

```bash
python3 tools/usb_intan_cmd.py "WRITE 64 0x80B4 0 0" --no-reset
python3 tools/usb_intan_cmd.py "WRITE 96 0x80B4 0 0" --no-reset
```

## Raw WRITE слова

Для быстрых стим-паттернов используйте `PATTERN_ADD_RAW`, а не `PATTERN_ADD_WRITE`.

`PATTERN_ADD_WRITE` разворачивает один `WRITE` в три SPI-слота:

```text
WRITE + dummy + dummy
```

Для triggered-регистров стимуляции нам достаточно raw `WRITE`-слова, потому что мы не читаем pipeline-ответ. Это уменьшает SPI-часть паттерна примерно в 3 раза.

Формат raw `WRITE` слова:

```text
word = (header << 24) | (reg << 16) | value
header = 0x80 | (U << 5) | (M << 4)
```

Подтверждённые рабочие слова для канала 0:

```text
0x802C0001   ; WRITE R44 = 0x0001, polarity mask ch0
0xA02A0001   ; WRITE R42 = 0x0001, U=1, ch0 ON
0xA02A0000   ; WRITE R42 = 0x0000, U=1, all OFF
```

Где:

```text
R42 = 0x2A
R44 = 0x2C
0x80 = WRITE без U/M
0xA0 = WRITE с U=1
```

## Безопасный шаблон запуска

Перед запуском:

```bash
python3 tools/usb_intan_cmd.py INIT_STIM --no-reset
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py CLEAR_COMP --no-reset
python3 tools/usb_intan_cmd.py "WRITE 64 0x80B4 0 0" --no-reset
python3 tools/usb_intan_cmd.py "WRITE 96 0x80B4 0 0" --no-reset
```

После запуска всегда:

```bash
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py READ 42 --no-reset
python3 tools/usb_intan_cmd.py READ 40 --no-reset
python3 tools/usb_intan_cmd.py READ 50 --no-reset
```

Ожидаемо:

```text
R42 = 0x0000
R40 = 0x0000
R50 = 0x0000
```

## Один импульс

Один импульс `ch0`, `180 мкА`, `duration_us`:

```bash
python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0x802C0001" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0001" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US <duration_us>" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0000" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US <duration_us>" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_RUN <repeat>" --no-reset --timeout-ms 20000
```

## Подтверждённый быстрый паттерн

Подтверждённый на осциллографе паттерн:

```text
500, 500,
200, 200,
100, 100,
50, 50,
20, 20,
10, 10 us
```

Каждое число означает пару:

```text
ON duration
OFF duration
```

Команды:

```bash
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py CLEAR_COMP --no-reset
python3 tools/usb_intan_cmd.py "WRITE 64 0x80B4 0 0" --no-reset
python3 tools/usb_intan_cmd.py "WRITE 96 0x80B4 0 0" --no-reset

python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0x802C0001" --no-reset

for us in 500 500 200 200 100 100 50 50 20 20 10 10; do
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0001" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US $us" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0000" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US $us" --no-reset
done

python3 tools/usb_intan_cmd.py PATTERN_STATUS --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_RUN 100" --no-reset --timeout-ms 20000
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py READ 42 --no-reset
python3 tools/usb_intan_cmd.py READ 40 --no-reset
python3 tools/usb_intan_cmd.py READ 50 --no-reset
```

Ожидаемый `PATTERN_STATUS` для одного блока:

```text
slots=49 spi=25 delays=24 err=0
```

## Как считать размер паттерна

Для raw-режима:

```text
1 polarity setup = 1 SPI slot
1 импульс = ON raw + ON delay + OFF raw + OFF delay = 4 slots
```

Для 12 импульсов:

```text
slots = 1 + 12 * 4 = 49
spi = 1 + 12 * 2 = 25
delays = 12 * 2 = 24
```

## Диагностика

Если на осциллографе ничего нет:

1. Проверьте, что нагрузка стоит между `elecN` и `stim_GND`.
2. Проверьте, что `stim_en` физически HIGH.
3. Проверьте, что смотрите правильный канал и правильную землю (`stim_GND`).
4. Запустите медленный тест `500 ms ON / 500 ms OFF`.
5. После запуска прочитайте:

```bash
python3 tools/usb_intan_cmd.py READ 40 --no-reset
python3 tools/usb_intan_cmd.py READ 50 --no-reset
```

Интерпретация:

```text
R40 != 0   ; compliance latch, нет нормального токового пути или упор в rail
R50 != 0   ; realtime fault current
R40 = 0 и R50 = 0, но сигнала нет ; вероятно stim_en, точка измерения или не тот вывод
```

## Что не делать

- Не ускорять generic `PATTERN_RUN` через DMA/grouped SPI transfer.
- Не держать CS низким на несколько 32-битных слов.
- Не использовать `PATTERN_ADD_WRITE` для быстрых triggered-регистров, если не нужен pipeline-ответ.
- Не оставлять `R42` включённым после теста.

