# Гайд: формирование stim-паттернов RHS2116 (USB V2)

Актуально для прошивки **WeActSTM32H743** (`0483:5741`, vendor bulk, текстовые команды на EP OUT, ответы на EP IN).

Исходники: `Core/Src/intan_pattern.c`, `Core/Src/usb_stream_service.c`, host: `tools/usb_intan_cmd.py`.

---

## 1. Идея

Паттерн — **очередь до 1024 слотов** в RAM. Каждый слот:

| Тип | Команда хоста | Действие прошивки |
|-----|---------------|-------------------|
| SPI | `PATTERN_ADD_RAW <word>` | один кадр `CS↓ → 32 bit → CS↑` (`Intan_Xfer32Word`) |
| SPI | `PATTERN_ADD_WRITE/READ/CONVERT/...` | **3 SPI-слота** (pipeline RHS2116) |
| Пауза | `PATTERN_ADD_DELAY_US <us>` | busy-wait, микросекунды |
| Пауза | `PATTERN_ADD_DELAY_CYC <cycles>` | busy-wait, циклы CPU |

`PATTERN_RUN <repeat>` выполняет всю очередь **repeat раз** подряд.

**Тактовая частота (дефолт проекта):** **SYSCLK = 480 MHz** (`BOARD_SYSCLK_480=ON`).  
`PATTERN_ADD_DELAY_US` считает `cycles = us × (SystemCoreClock / 1e6)` → при 480 MHz **100 µs = 48 000 циклов DWT**, wall-time **~100 µs**. SPI SCK по-прежнему **25 MHz** (PLL2 не меняется). Проверка: `STATS` → `sysclk_mhz=480`.

**Инвариант стимуляции:** каждое 32-битное слово RHS2116 — **отдельная CS-транзакция**. Для triggered-стима используйте **`PATTERN_ADD_RAW`**, не DMA/grouped SPI и не `PATTERN_ADD_WRITE` (он в 3× длиннее).

---

## 2. USB: что слать с хоста

| Параметр | Значение |
|----------|----------|
| VID:PID | `0483:5741` |
| Команда | EP OUT `0x01`, строка ASCII + `\n` |
| Ответ | EP IN `0x81`, до ~512 B текста (`OK …` / `ERR …`) |
| Длинный `PATTERN_RUN` | `--timeout-ms 60000` и более |

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0004" --no-reset
```

Программно (PyUSB):

```python
import sys
sys.path.insert(0, "tools")
from usb_intan_lib import open_device, run_text_command, close_device

dev, ifn = open_device(reset=False)
reply = run_text_command(dev, "PATTERN_STATUS", timeout_ms=5000)
close_device(dev, ifn)
```

**Важно:** во время `PATTERN_RUN` bulk IN **не** шлёт RHS1-кадры — только финальный текстовый ответ. Если до этого шёл `SPI_STREAM_*`, прошивка **сама останавливает stream** перед любой `PATTERN_*` (commit `caeca44`).

---

## 3. Полный список команд

### Подготовка (вне паттерна)

```text
INIT_STIM
WRITE <reg> <value> <u> <m>
CLEAR_COMP
READ <reg>
```

### Сборка и запуск паттерна

```text
PATTERN_CLEAR
PATTERN_ADD_RAW <word>
PATTERN_ADD_WRITE <reg> <value> <u> <m>   # 3 SPI-слота; для стима обычно не нужен
PATTERN_ADD_READ <reg>
PATTERN_ADD_CONVERT <ch> <flags>
PATTERN_ADD_CLEAR_ADC
PATTERN_ADD_CLEAR_COMP
PATTERN_ADD_DELAY_CYC <cycles>
PATTERN_ADD_DELAY_US <us>
PATTERN_STATUS
PATTERN_RUN <repeat>                      # repeat: 1..10000
```

### Ответы прошивки

| Команда | Пример ответа |
|---------|----------------|
| `PATTERN_CLEAR` | `OK PATTERN_CLEAR` |
| `PATTERN_ADD_*` | `OK PATTERN_ADD_RAW` / `ERR pattern_add` |
| `PATTERN_STATUS` | `OK PATTERN_STATUS loaded=1 running=0 slots=81 spi=41 delays=40 err=0` |
| `PATTERN_RUN 10` | `OK PATTERN_RUN` / `ERR pattern_run` |

---

## 4. Регистры RHS2116 (стим)

| Reg | Назначение |
|-----|------------|
| 32, 33 | unlock stim (`INIT_STIM` пишет `0xAAAA` / `0x00FF`) |
| **42** | triggered ON/OFF (маска каналов), **U=1** для update-now |
| **44** | triggered polarity (маска каналов) |
| **64..79** | negative current magnitude, **reg = 64 + ch** |
| **96..111** | positive current magnitude, **reg = 96 + ch** |
| 40 | compliance monitor latch |
| 50 | realtime fault current |

**Железо:** `stim_en` = HIGH; ток измерять между **`elecN`** и **`stim_GND`**.

### Кодирование тока

```text
reg_value = 0x8000 | current_uA
```

Пример **180 µA**: `0x8000 | 180 = 0x80B4`.

---

## 5. Raw WRITE-слова для паттерна

```text
word = (header << 24) | (reg << 16) | value
header = 0x80 | (U << 5) | (M << 4)
```

| header | Значение |
|--------|----------|
| `0x80` | WRITE, без U/M |
| `0xA0` | WRITE, **U=1** (update triggered registers now) |

Для канала `ch`: **mask = 1 << ch**.

| Действие | reg | value | Формула word |
|----------|-----|-------|--------------|
| Polarity | 44 (`0x2C`) | mask | `0x802C0000 \| mask` |
| ON | 42 (`0x2A`) | mask | `0xA02A0000 \| mask` |
| OFF | 42 (`0x2A`) | 0 | `0xA02A0000` |

### Таблица для ch0..ch3

| ch | mask | R44 polarity | R42 ON | R42 OFF |
|----|------|--------------|--------|---------|
| 0 | `0x0001` | `0x802C0001` | `0xA02A0001` | `0xA02A0000` |
| 1 | `0x0002` | `0x802C0002` | `0xA02A0002` | `0xA02A0000` |
| 2 | `0x0004` | `0x802C0004` | `0xA02A0004` | `0xA02A0000` |
| 3 | `0x0008` | `0x802C0008` | `0xA02A0008` | `0xA02A0000` |

Регистры тока (вне паттерна, через `WRITE`):

```text
WRITE (64+ch) 0x80B4 0 0    # negative 180 µA
WRITE (96+ch) 0x80B4 0 0    # positive 180 µA
```

---

## 6. Шаблон: один импульс

Один импульс на **ch**, ток задаётся заранее через `WRITE`, длительность — `duration_us`:

```text
PATTERN_CLEAR
PATTERN_ADD_RAW 0x802C{mask}     ; polarity
PATTERN_ADD_RAW 0xA02A{mask}     ; ON
PATTERN_ADD_DELAY_US <duration_us>
PATTERN_ADD_RAW 0xA02A0000       ; OFF
PATTERN_ADD_DELAY_US <duration_us>
PATTERN_RUN <repeat>
```

После **`PATTERN_RUN`** прошивка **сама** шлёт полный `WRITE R42=0 U=1` (3 CS-слота), даже если последний `PATTERN_ADD_RAW OFF` не погасил выход. Дополнительно на хосте: `WRITE 42 0 1 0`.

---

## 7. Пример: ch2, 180 µA, импульсы 100 µs

### 7.1 Подготовка

```bash
python3 tools/usb_intan_cmd.py INIT_STIM --no-reset
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py CLEAR_COMP --no-reset
python3 tools/usb_intan_cmd.py "WRITE 66 0x80B4 0 0" --no-reset   # reg 64+2, neg
python3 tools/usb_intan_cmd.py "WRITE 98 0x80B4 0 0" --no-reset   # reg 96+2, pos
python3 tools/usb_intan_cmd.py READ 66 --no-reset
python3 tools/usb_intan_cmd.py READ 98 --no-reset
```

### 7.2 Паттерн: 20 импульсов 100 µs ON / 100 µs OFF

```bash
python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0x802C0004" --no-reset

# 20 раз:
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0004" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US 100" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0000" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US 100" --no-reset

python3 tools/usb_intan_cmd.py PATTERN_STATUS --no-reset
# OK PATTERN_STATUS loaded=1 running=0 slots=81 spi=41 delays=40 err=0

python3 tools/usb_intan_cmd.py "PATTERN_RUN 100" --no-reset --timeout-ms 60000
```

Период одного импульса: **200 µs** → **5 kHz** в burst.  
100 повторов паттерна → **2000 импульсов** на ch2.

### 7.3 Выключение и диагностика

```bash
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py READ 42 --no-reset   # ожидаем 0x0000
python3 tools/usb_intan_cmd.py READ 40 --no-reset
python3 tools/usb_intan_cmd.py READ 50 --no-reset
```

---

## 8. Пример: sweep длительностей (осциллограф)

Подтверждённый паттерн с убывающими паузами (ch0, 180 µA):

```text
500, 500, 200, 200, 100, 100, 50, 50, 20, 20, 10, 10 µs
```

(каждое число — ON duration, затем то же для OFF)

```bash
python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0x802C0001" --no-reset

for us in 500 500 200 200 100 100 50 50 20 20 10 10; do
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0001" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US $us" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0000" --no-reset
  python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US $us" --no-reset
done

python3 tools/usb_intan_cmd.py PATTERN_STATUS --no-reset
# slots=49 spi=25 delays=24 err=0

python3 tools/usb_intan_cmd.py "PATTERN_RUN 100" --no-reset --timeout-ms 60000
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
```

---

## 9. Размер паттерна (raw-стим)

```text
1 polarity setup     = 1 SPI slot
1 импульс            = ON raw + ON delay + OFF raw + OFF delay = 4 slots

slots  = 1 + N_impulses × 4
spi    = 1 + N_impulses × 2
delays = N_impulses × 2
```

| N импульсов | slots | spi | delays |
|-------------|-------|-----|--------|
| 1 | 5 | 3 | 2 |
| 12 | 49 | 25 | 24 |
| 20 | 81 | 41 | 40 |

Лимит: **1024 слота** → максимум **~255 импульсов** в одном паттерне (raw).

---

## 10. Python: сборка паттерна программно

```python
def stim_raw_words(ch: int) -> tuple[int, int, int]:
    mask = 1 << ch
    return (
        0x802C0000 | mask,   # polarity
        0xA02A0000 | mask,   # ON
        0xA02A0000,          # OFF
    )

def build_pulse_pattern(dev, ch: int, pulse_us: int, n_pulses: int, run_text_command):
    pol, on, off = stim_raw_words(ch)
    run_text_command(dev, "PATTERN_CLEAR")
    run_text_command(dev, f"PATTERN_ADD_RAW {pol}")
    for _ in range(n_pulses):
        run_text_command(dev, f"PATTERN_ADD_RAW {on}")
        run_text_command(dev, f"PATTERN_ADD_DELAY_US {pulse_us}")
        run_text_command(dev, f"PATTERN_ADD_RAW {off}")
        run_text_command(dev, f"PATTERN_ADD_DELAY_US {pulse_us}")

def setup_stim_ch(dev, ch: int, current_uA: int, run_text_command):
    val = 0x8000 | current_uA
    for cmd in [
        "INIT_STIM",
        "WRITE 42 0 1 0",
        "CLEAR_COMP",
        f"WRITE {64+ch} {val} 0 0",
        f"WRITE {96+ch} {val} 0 0",
    ]:
        run_text_command(dev, cmd)
```

---

## 11. Диагностика

| Симптом | Вероятная причина |
|---------|-------------------|
| `ERR pattern_run` | пустой паттерн (`PATTERN_CLEAR` без ADD), SPI не ready |
| `ERR pattern_add` | >1024 слотов или паттерн уже running |
| Ответ `1SHR…` вместо `OK` | **старый баг**: stream не остановлен; в актуальной прошивке исправлено |
| `R42 ≠ 0` после OFF | не выполнен `WRITE 42 0 1 0` |
| `R50 ≠ 0` | fault current (КЗ, превышение) |
| `R40 ≠ 0` | compliance latch — нет нагрузки, упор в rail, или обрыв |
| `R40=1`, `R50=0`, сигнала нет | проверить `stim_en`, `stim_GND`, правильный `elecN` |

Рекомендуемый медленный тест: **500 ms ON / 500 ms OFF** (`PATTERN_ADD_DELAY_US 500000`).

---

## 12. Чего не делать

- Не ускорять `PATTERN_RUN` через TIM/DMA/grouped SPI — ломает стим-сигнал.
- Не держать CS низким на несколько 32-битных слов подряд.
- Не использовать `PATTERN_ADD_WRITE` для быстрых R42/R44 toggles.
- Не оставлять `R42` включённым после теста.
- Не парсить ответы паттерна из bulk IN во время активного `SPI_STREAM_*` без `drain` — сначала `STOP` или дождитесь завершения stream-команды.

---

## 13. Связанные файлы

| Файл | Содержание |
|------|------------|
| `Core/Inc/intan_pattern.h` | API, лимит 1024 слотов |
| `Core/Src/intan_pattern.c` | очередь, `Intan_Pattern_Run` |
| `Core/Src/usb_commands.c` | разбор текстовых команд |
| `tools/usb_intan_cmd.py` | CLI для хоста |
| `AGENTS.md` | общий контекст USB V2 |
