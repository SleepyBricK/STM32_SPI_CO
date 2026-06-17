# Гайд по тестированию Intan RHS2116 (STM32 + Pi + GUI + осциллограф)

Практический порядок проверки **стим-паттернов**, **таймингов** и **импеданса** для связки:

```text
ПК ──USB── STM32 (0483:5741) ──SPI── Intan RHS2116
ПК ──USB── Moku Go (осциллограф на нагрузке)
ПК ──TCP:9000── Orange Pi ──USB── STM32   (опционально, через GUI)
```

Типовая нагрузка для проверки: **10 kΩ между ch2 и GND**, ток **180 µA**, импульс **`PATTERN_ADD_DELAY_US 100`**.

**См. также:**

| Тема | Файл |
|------|------|
| Формат `PATTERN_*`, RAW-слова | [`intan_stim_pattern_guide.md`](intan_stim_pattern_guide.md) |
| Orange Pi + GUI | [`intan_pi_gui_guide.md`](intan_pi_gui_guide.md) |
| `IMPEDANCE_MEASURE` | [`intan_impedance_guide.md`](intan_impedance_guide.md) |
| Wall-clock bench | [`tools/test_pattern_timing.py`](tools/test_pattern_timing.py) |

---

## 1. Что проверяем

| Уровень | Вопрос | Инструмент |
|---------|--------|------------|
| **A. USB / прошивка** | `PATTERN_ADD_DELAY_US` даёт нужные µs? | `tools/usb_intan_cmd.py`, wall-clock |
| **B. Pi + GUI** | Тот же паттерн доходит до STM32? | `intan_server --backend usb`, GUI |
| **C. Осциллограф** | Импульс на нагрузке совпадает с командой? | Moku Go Oscilloscope |
| **D. Импеданс** | Zcheck ≈ 10 kΩ на ch2? | `IMPEDANCE_MEASURE` / GUI «Измерения» |

---

## 2. Железо и подключение

### 2.1. Стим (ch2, 10 kΩ)

- **Нагрузка:** 10 kΩ между **elec2** и **GND** (stim_GND).
- **`stim_en`:** HIGH.
- Ток измерять между **elec2** и **stim_GND** (не между elec2 и другим elec).
- **Moku Go:** вход на нагрузке (напряжение на 10 kΩ). При 180 µA ожидайте **~1.8 mV** → диапазон **100 mVpp** или **400 mVpp**, DC coupling.

### 2.2. USB-цепочка

| Устройство | VID:PID / адрес |
|------------|-----------------|
| STM32 | `0483:5741` (USB HS) |
| Moku Go | `33e2:0028` (NCM, IPv6 link-local) |

### 2.3. Moku Go (Python SDK)

```bash
mokucli list
```

Пример:

```text
MokuGo-002464  2464  Go  644  fe80::7269:79ff:feb9:2682%41
```

Подключение (Mac, USB):

```python
from moku.instruments import Oscilloscope

osc = Oscilloscope('[fe80::7269:79ff:feb9:2682%41]', force_connect=True)
# или serial=2464, если lookup работает
```

После теста: `osc.relinquish_ownership()`.

> IPv6 link-local: адрес в **квадратных скобках**, со scope id (`%41` или имя интерфейса). Подробности: [Moku API — IP address](https://apis.liquidinstruments.com/api/getting-started/ip-address.html).

---

## 3. Pre-flight (5 минут)

### 3.1. STM32

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset
python3 tools/usb_intan_cmd.py ID --no-reset
```

Ожидаемо:

| Поле | Значение |
|------|----------|
| `PING` | `PONG` |
| `sysclk_mhz` | **480** (дефолт проекта) |
| `sck_khz` | **25000** |
| `ID` | chip=32 (RHS2116) |

### 3.2. Orange Pi (если тест через GUI)

```bash
python3 intan_server.py --backend usb --verbose
lsusb -d 0483:5741
```

### 3.3. Prep стима (перед паттерном)

```bash
python3 tools/usb_intan_cmd.py INIT_STIM --no-reset
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py CLEAR_COMP --no-reset
```

На Pi то же выполняется в `_prepare_pattern_load_state()` при `pattern_load`.

---

## 4. Тест A — wall-clock (без осциллографа)

Скрипт: **`tools/test_pattern_timing.py`**

```bash
python3 tools/test_pattern_timing.py --no-reset
```

### Что измеряет

Время ответа USB на `PATTERN_RUN N` в зависимости от `N`. По **наклону** (разница между `RUN 50` и `RUN 1`, делённая на число итераций) видно, сколько µs реально тратит DWT на каждый `PATTERN_ADD_DELAY_US`.

### Эталонный паттерн (GUI, ch2, 100 µs)

```text
PATTERN_ADD_RAW 0x804280B4    # R66 ch2 neg 180 µA
PATTERN_ADD_RAW 0x806280B4    # R98 ch2 pos 180 µA
PATTERN_ADD_RAW 0x802C0004    # R44 polarity ch2
PATTERN_ADD_RAW 0xA02A0004    # R42 ON ch2 U=1
PATTERN_ADD_DELAY_US 100
PATTERN_ADD_RAW 0xA02A0000    # R42 OFF
```

### Критерии прошивки

| Тест | Ожидание |
|------|----------|
| `PATTERN_ADD_DELAY_US 1000`, RUN 50 | slope ≈ **1000 µs/iter** |
| `PATTERN_ADD_DELAY_US 100`, RUN 1000 | slope ≈ **100 µs + ~150 µs SPI** ≈ **250 µs/iter** |
| `PATTERN_STATUS` для паттерна выше | **slots=6**, spi=5, delays=1, err=0 |

### Важно: фиксированный overhead ~57 ms

**Первый** `PATTERN_RUN` после загрузки паттерна включает **~57 ms** одноразового overhead (`Intan_DmaPathRelease()` перед циклом в `Intan_Pattern_Run`). Это **не** длительность импульса на осциллографе.

Для оценки delay используйте **RUN ≥ 50** и считайте **slope**, не абсолютное время `RUN 1`:

```text
per_iter ≈ (T_run50 - T_run1) / 49
```

Реализация delay в прошивке (`Core/Src/intan_pattern.c`):

```text
cycles = delay_us × (SystemCoreClock / 1_000_000)
```

При `sysclk_mhz=480`: **100 µs → 48 000 циклов DWT**.

---

## 5. Тест B — ручной USB + PATTERN_STATUS

```bash
python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
# ... все PATTERN_ADD_* ...
python3 tools/usb_intan_cmd.py PATTERN_STATUS --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_RUN 100" --no-reset --timeout-ms 60000
python3 tools/usb_intan_cmd.py READ 42 --no-reset   # ожидаем 0x0000
python3 tools/usb_intan_cmd.py READ 40 --no-reset
python3 tools/usb_intan_cmd.py READ 50 --no-reset
```

### Слоты vs строки в редакторе

| Команда | Слотов в RAM |
|---------|--------------|
| `PATTERN_ADD_RAW` | 1 |
| `PATTERN_ADD_DELAY_US` | 1 |
| `PATTERN_ADD_WRITE` | 3 |
| Pi auto safety `PATTERN_ADD_WRITE 42 0 1 0` | +3, если нет в конце паттерна |

**`commands_count` / `slots_count` от Pi = слоты RAM STM32**, не число строк в GUI.

Формула для raw-стима (N импульсов, без trailing pause):

```text
slots = 1 (polarity) + N × 4 + [+3 safety OFF на сервере]
```

---

## 6. Тест C — GUI / Orange Pi

### 6.1. Порядок в GUI

1. Подключиться к Pi (TCP **9000**).
2. «📄 Пример» или паттерн из редактора.
3. **Загрузить паттерn** → TCP `pattern_load`.
4. **Запустить** → TCP `pattern_run` с `repeat_count`.

Файл GUI: `msu-neuro-terminal-linux/services/gui/intan_gui_new.py`.

### 6.2. Что сверить в логе

- `commands_count` ≈ оценка слотов GUI (~**7** для одного импульса 100 µs: 6 + auto safety +3, если safety добавляет сервер).
- Только **`PATTERN_ADD_DELAY_US`**, не legacy **`DELAY`**.
- **Нет паузы после последнего OFF** (лишний хвост на осцилле).

### 6.3. Типичные ошибки Pi/GUI

| Симптом | Причина |
|---------|---------|
| Пауза ~300 µs при команде 100 µs между импульсами | **100 µs delay + ~150 µs SPI** (OFF + re-ON) — норма для wall-clock |
| `DELAY 500` в паттерне | На STM32 → **640 µs** (N × 1.28 µs SPI-slot), **не** 500 ms |
| Слотов в GUI ≠ Pi | Считать **слоты**; +3 auto safety на сервере |
| `PATTERN_ADD_WRITE` не в load | Должен проходить в `pattern_load` (исправлено в GUI) |

---

## 7. Тест D — Moku Go (осциллограф)

### 7.1. Настройка Moku (Python)

```python
from moku.instruments import Oscilloscope

osc = Oscilloscope('[fe80::7269:79ff:feb9:2682%41]', force_connect=True)
osc.set_defaults()
osc.set_frontend(1, impedance='1MOhm', coupling='DC', range='100mVpp')
osc.set_sources(['Input1', 'Input1'])
osc.set_trigger(
    type='Edge', source='Input1', edge='Rising',
    level=0.0005, mode='Normal',
)
osc.set_timebase(t1=-0.0002, t2=0.002, max_length=8192)  # 2 ms окно
```

Уровень триггера подстроить под амплитуду на 10 kΩ (~1–2 mV при 180 µA).

### 7.2. Протокол одного прогона

1. Загрузить паттерн на STM32 (USB или GUI).
2. Arm Moku: `get_data(wait_reacquire=True, wait_complete=True)`.
3. **`PATTERN_RUN 10`** (или 100) — burst импульсов.
4. Снять waveform, измерить:
   - **ширину импульса** (ON → OFF, порог 50%);
   - **интервал** между фронтами соседних импульсов.

### 7.3. Ожидаемые значения (ch2, 100 µs ON, без trailing pause)

| Параметр | Команда | Ожидание на scope |
|----------|---------|-------------------|
| Длительность ON | `PATTERN_ADD_DELAY_US 100` после ON | **~100 µs** ± SPI (~2–5 µs) |
| Между импульсами (2 в паттерне) | pause 100 µs после OFF | **~250–300 µs** (100 µs + SPI) |
| Амплитуда на 10 kΩ | 180 µA | **~1.8 mV** |

### 7.4. Диагностика «30 µs импульс / 300 µs пауза»

| Наблюдение | Интерпретация |
|------------|---------------|
| **30 µs** импульс при `100 µs` | Проверить триггер/полосу Moku; при wall-clock slope≈100 µs прошивка OK → смотреть аналог, Reg34 step, FWHM spike |
| **300 µs** между импульсами при pause 100 µs | Часто **100 µs + SPI re-ON**, не «ошибка ×3» |
| `PATTERN_RUN 1` «долго» (~57 ms) | Overhead `Intan_DmaPathRelease`, не длина импульса |

**Разделение:**

1. **Wall-clock slope ≈ заданным µs** → DWT/STM32 OK.
2. **Scope ≠ wall-clock** → настройка Moku, точка измерения, форма сигнала RHS2116.
3. **GUI ≠ USB direct** → лог `pattern_load`, `PATTERN_STATUS`, legacy `DELAY`.

---

## 8. Тест E — импеданс (ch2, 10 kΩ)

### 8.1. Прямой USB

```bash
python3 tools/usb_intan_cmd.py INIT_RECORD 610 --no-reset
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 2 1 1000 16 128 3" --no-reset --timeout-ms 180000
```

Критерии качества в каждом ответе:

```text
overruns=0
spi_errors=0
clipped=0
actual_freq_millihz=1000000
```

Ожидание: **Z ≈ 8–10 kΩ**, `V_amp ≈ 30–40 µV` (scale 1 pF, 1 kHz).

### 8.2. Через GUI

Нужен Pi с `measure_impedance_fast` → прокси `IMPEDANCE_MEASURE` (ветка `dev_actual` на GitLab).

---

## 9. Матрица тестов (чеклист)

| # | Тест | Действие | Pass |
|---|------|----------|------|
| 1 | USB alive | `PING`, `STATS` | sysclk=480, sck=25M |
| 2 | Delay 1 ms | `test_pattern_timing.py` baseline | slope ≈ 1000 µs |
| 3 | Delay 100 µs | тот же скрипт, ch2 pattern | slope ≈ 250 µs |
| 4 | PATTERN_STATUS | slots match estimate | err=0 |
| 5 | Scope 100 µs | Moku pulse width | ~100 µs ±10% |
| 6 | Burst 10 pulses | period stable | нет drift / залипания |
| 7 | Safe OFF | `READ 42` после RUN | 0x0000 |
| 8 | Impedance ch2 | `IMPEDANCE_MEASURE 2 …` | ~10 kΩ, overruns=0 |
| 9 | Pi path | GUI load + run | slots_count ≈ GUI estimate |

---

## 10. Sweep для калибровки таймингов

После базового 100 µs — sweep (как в GUI «📄 Пример»):

```text
500, 500, 200, 200, 100, 100, 50, 50, 20, 20, 10, 10 µs
```

(каждое число — длительность ON, затем такая же OFF-пауза между импульсами)

На Moku для каждой пары ON/OFF записать **измеренную** vs **заданную** → таблица калибровки.

| Задано (µs) | Scope ON (µs) | Scope gap (µs) | Wall slope (µs) |
|-------------|---------------|----------------|-----------------|
| 500 | | | |
| 100 | | | |
| 10 | | | |

- Линейная ошибка на всех точках → искать **SystemCoreClock** / прошивку.
- Только scope расходится → **Moku setup + аналог RHS2116**.

---

## 11. Рекомендуемый порядок одного сеанса

1. **STATS** → подтвердить 480 MHz.
2. **`test_pattern_timing.py`** → slope delays OK.
3. **Moku** → один импульс 100 µs, затем 500 µs (медленный, удобный для триггера).
4. **GUI/Pi** (если используете) → тот же паттерн, сверить `slots_count`.
5. **`IMPEDANCE_MEASURE ch2`** → 10 kΩ.
6. Заполнить таблицу sweep (§10).

---

## 12. Связанные файлы

| Путь | Содержание |
|------|------------|
| `Core/Src/intan_pattern.c` | Очередь паттерна, DWT delay |
| `Core/Src/intan_spi.c` | `Intan_DmaPathRelease`, SPI/CS |
| `tools/test_pattern_timing.py` | Wall-clock bench |
| `tools/usb_intan_cmd.py` | CLI USB |
| `msu-neuro-terminal-linux/services/gui/intan_gui_new.py` | GUI паттернов |
| `msu-neuro-terminal-linux/services/server/intan_tcp_server.py` | `pattern_load` / `pattern_run` |
