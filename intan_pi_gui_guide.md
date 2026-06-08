# Гайд: Orange Pi + GUI — паттерны стимуляции и импеданс

Актуально для связки:

```text
ПК (intan_gui_new.py) ──TCP :9000──► Orange Pi (intan_server --backend usb)
                                           │
                                           └──USB HS 0483:5741──► STM32H743 ──SPI──► Intan RHS2116
```

Прошивка STM32 выполняет реальную работу с Intan. Orange Pi — **тонкий USB-мост** (текстовые команды EP OUT → ответ EP IN). GUI не ходит на SPI/GPIO Pi напрямую.

**См. также (низкий уровень, прошивка):**

| Тема | Файл |
|------|------|
| Формат `PATTERN_*`, RAW-слова, R42/R44 | [`intan_stim_pattern_guide.md`](intan_stim_pattern_guide.md) |
| `IMPEDANCE_MEASURE`, Zcheck, пересчёт в Ω | [`intan_impedance_guide.md`](intan_impedance_guide.md) |
| USB V2, bench | [`AGENTS.md`](AGENTS.md) |

---

## 1. Архитектура и роли

```mermaid
flowchart LR
  GUI["intan_gui_new.py\n(PК, Tk)"]
  TCP["intan_server\n--backend usb"]
  USB["STM32 0483:5741\nvendor bulk"]
  INTAN["RHS2116 SPI"]

  GUI -->|"JSON TCP :9000"| TCP
  TCP -->|"ASCII + \\n"| USB
  USB --> INTAN
```

| Уровень | Ответственность |
|---------|-----------------|
| **GUI** | Генерация `PATTERN_ADD_*`, prep `INIT_STIM`, JSON-команды `pattern_load` / `pattern_run` / `measure_impedance_fast` |
| **Pi server** | Проброс строк на USB, таймауты, парсинг `OK IMPEDANCE …`, JSON для GUI |
| **STM32** | Очередь паттерна (до 1024 слотов), `IMPEDANCE_MEASURE`, CS-safe SPI |

**Важно:** старый Pi-сервер с `/dev/intan` и `WRITE`/`DELAY` (SPI steps) **не совместим** с текущим GUI для паттернов. GUI шлёт **`PATTERN_ADD_RAW`** / **`PATTERN_ADD_DELAY_US`** — формат прошивки `Core/Src/intan_pattern.c`.

---

## 2. Запуск на Orange Pi

### 2.1. Требования

- Orange Pi с USB HS к STM32 (`lsusb` → `0483:5741`, скорость **480M** в `lsusb -t`)
- Python 3 + `pyusb`
- Прошивка `WeActSTM32H743` с USB V2 (ответ на `PING`)

### 2.2. Проверка USB без GUI

На Pi (или с Mac, если STM32 подключён локально):

```bash
python3 tools/usb_intan_cmd.py PING
python3 tools/usb_intan_cmd.py STATS --no-reset    # sysclk_mhz=480 — норма
python3 tools/usb_intan_cmd.py ID --no-reset
```

### 2.3. Сервер для GUI

```bash
# На Orange Pi, из каталога с intan_server.py:
python3 intan_server.py --backend usb --tcp-port 9000 --verbose
```

Флаг **`--backend usb`** обязателен: SPI Intan на Pi **не используется**, все команды идут на STM32.

> **Статус репозитория:** эталонный `msu-neuro-terminal-linux/services/server/intan_server.py` в git — legacy (`/dev/spidev`). USB-backend должен быть на Pi отдельно или добавлен в fork; контракт ниже — **то, что ожидает `intan_gui_new.py`**.

### 2.4. systemd (пример)

```ini
[Service]
ExecStart=/usr/bin/python3 /opt/intan/intan_server.py --backend usb --tcp-port 9000
Restart=on-failure
```

---

## 3. TCP JSON API (контракт GUI ↔ Pi)

Транспорт: **TCP**, порт **9000**, одна JSON-строка + `\n` на запрос и ответ.

### 3.1. Общие поля

| Поле | Значение |
|------|----------|
| Успех | `"status": "ok"` |
| Ошибка | `"status": "error"`, `"error": "текст"` |

### 3.2. `send_line` — проброс одной команды STM32

**Запрос:**

```json
{"cmd": "send_line", "line": "INIT_STIM"}
```

**Ответ:**

```json
{"status": "ok", "cmd": "send_line", "response": "OK INIT_STIM"}
```

Pi должен передать `line` на EP OUT STM32 и вернуть текст ответа (до ~512 B) в `response`.

**Команды, нужные GUI для стима:**

| Строка | Назначение |
|--------|------------|
| `INIT_STIM` | unlock R32/R33 |
| `WRITE 42 0 1 0` | safe OFF triggered stim |
| `CLEAR_COMP` | сброс compliance latch |
| `READ <reg>` | диагностика после RUN |
| `IMPEDANCE_MEASURE …` | см. §6 |

Таймаут GUI: **15 s** (`TCP_TIMEOUT_SEND_LINE`). Для длинных команд — больше.

### 3.3. `pattern_load` — загрузка очереди в RAM STM32

**Запрос:**

```json
{
  "cmd": "pattern_load",
  "commands": [
    "PATTERN_ADD_RAW 0x802C0004",
    "PATTERN_ADD_RAW 0xA02A0004",
    "PATTERN_ADD_DELAY_US 100",
    "PATTERN_ADD_RAW 0xA02A0000",
    "PATTERN_ADD_WRITE 42 0 1 0"
  ]
}
```

**Ответ:**

```json
{
  "status": "ok",
  "cmd": "pattern_load",
  "commands_count": 5,
  "message": "Паттерн загружен в память: 5 команд"
}
```

**Алгоритм на Pi (USB-backend):**

1. `PATTERN_CLEAR` → дождаться `OK PATTERN_CLEAR`
2. Для каждой строки из `commands`: отправить как есть → `OK PATTERN_ADD_*` / `ERR pattern_add`
3. Опционально: `PATTERN_STATUS` → лог `slots=…`
4. Вернуть `commands_count = len(commands)`

**Не путать** со старым `pattern_load` на `/dev/intan`: там `WRITE`/`DELAY` компилировались в driver ops. Сейчас список — **уже готовые команды прошивки**.

Лимит: **1024 слота** в RAM STM32. GUI оценивает слоты до отправки (`estimate_stm32_pattern_slots`).

### 3.4. `pattern_run` — выполнение загруженного паттерна

**Запрос:**

```json
{"cmd": "pattern_run", "repeat_count": 100}
```

**Ответ:**

```json
{"status": "ok", "cmd": "pattern_run", "repeat_count": 100}
```

**На Pi:**

```text
PATTERN_RUN <repeat_count>
```

Таймаут: **≥ 30 s**, для длинных паттернов — `repeat × (сумма delay + SPI)`; GUI использует `max(30, repeat × 0.5)` секунд.

После `PATTERN_RUN` прошивка **сама** шлёт полный `WRITE R42=0 U=1` (3 CS). Дополнительно можно `send_line("WRITE 42 0 1 0")`.

### 3.5. `measure_impedance_fast` — импеданс для GUI

**Запрос (как шлёт GUI):**

```json
{
  "cmd": "measure_impedance_fast",
  "channel": 2,
  "frequency": 1000,
  "scale": "1 pF",
  "num_averages": 1,
  "num_samples": 64,
  "auto_scale": false,
  "include_points": true
}
```

**Ответ (минимум для GUI):**

```json
{
  "status": "ok",
  "cmd": "measure_impedance_fast",
  "impedance_ohm": 9800.0,
  "std_dev_ohm": 0.0,
  "channel": 2,
  "frequency": 1000.0,
  "scale": "1 pF",
  "num_valid": 1,
  "v_amp_uv": 37.7,
  "phase_deg": -15.9,
  "likely_floating": false,
  "points": [],
  "valid_z": []
}
```

Таймаут GUI: **до 180 s** (`TCP_TIMEOUT_IMPEDANCE`).

**Реализация на Pi (USB-backend):** см. §6.

### 3.6. Прочие команды GUI

| `cmd` | Назначение |
|-------|------------|
| `ping` | `"reply": "pong"` |
| `read_register` | `{"address": 255, "value": 32}` |
| `stop` | safe OFF всех каналов (legacy pulse path) |
| `configure_adc` | `INIT_RECORD` profile через USB |

---

## 4. Паттерны: workflow в GUI

Файл: **`intan_gui_new.py`**, вкладка «Паттерны».

### 4.1. Три этапа (как в редакторе)

```text
§1 Подготовка     → send_line (вне pattern_load)
§2 pattern_load   → PATTERN_ADD_* в RAM STM32
§3 pattern_run    → PATTERN_RUN <repeat>
```

**§1 — prep** (кнопка «Загрузить паттерн», до `pattern_load`):

```text
INIT_STIM
WRITE 42 0 1 0
CLEAR_COMP
```

**§2 — тело патterna** (уходит в `pattern_load`):

- Токи: `PATTERN_ADD_RAW` для R64/R96 (`0x8000 | µA`)
- Полярность: `PATTERN_ADD_RAW` R44 (`0x802Cxxxx`)
- Импульс: **ON → delay → raw OFF → delay** (у **последнего** импульса **нет** delay после OFF)
- Финал: **`PATTERN_ADD_WRITE 42 0 1 0`** (3 CS, надёжный OFF)

**§3 — запуск** (кнопка «Запустить»):

```text
PATTERN_RUN 100
```

### 4.2. Формат одного импульса (GUI)

```text
PATTERN_ADD_RAW 0x802C0004          # R44 polarity ch2
PATTERN_ADD_RAW 0xA02A0004          # R42 ON ch2, U=1
PATTERN_ADD_DELAY_US 100
PATTERN_ADD_RAW 0xA02A0000          # R42 raw OFF
PATTERN_ADD_DELAY_US 100            # только между импульсами
...
PATTERN_ADD_WRITE 42 0 1 0          # safety OFF (3 CS)
```

Кодирование RAW-слова:

```text
word = (header << 24) | (reg << 16) | value
header = 0x80 | (U << 5) | (M << 4)
R42 ON chN: reg=42, value=(1<<N), U=1 → 0xA02A000N
```

### 4.3. Кнопки GUI

| Кнопка | Действие |
|--------|----------|
| **Загрузить паттерн** | §1 `send_line` → TCP `pattern_load` |
| **Запустить** | TCP `pattern_run` с `repeat_count` из поля |
| **📄 Пример** | sweep 500→200→100→50→20→10 µs, ch2, 180 µA |
| **Очистить** | только редактор |

### 4.4. Оценка слотов

| Команда | Слотов в RAM |
|---------|--------------|
| `PATTERN_ADD_RAW` | 1 |
| `PATTERN_ADD_DELAY_US` / `_CYC` | 1 |
| `PATTERN_ADD_WRITE` / `READ` / `CONVERT` | 3 |

Максимум: **1024**. При превышении GUI блокирует загрузку.

### 4.5. Проверка с Pi вручную (без GUI)

```bash
# На Pi, через tools/ (скопировать usb_intan_lib.py + usb_intan_cmd.py):
python3 tools/usb_intan_cmd.py INIT_STIM --no-reset
python3 tools/usb_intan_cmd.py "WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py CLEAR_COMP --no-reset
python3 tools/usb_intan_cmd.py PATTERN_CLEAR --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0x802C0004" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0004" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_DELAY_US 100000" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_RAW 0xA02A0000" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_ADD_WRITE 42 0 1 0" --no-reset
python3 tools/usb_intan_cmd.py "PATTERN_RUN 10" --no-reset --timeout-ms 60000
python3 tools/usb_intan_cmd.py READ 42 --no-reset   # ожидаем 0x0000
```

Осциллограф: **`stim_en` HIGH**, ток между **`elecN`** и **`stim_GND`**.

### 4.6. Типичные ошибки (паттерны)

| Симптом | Причина | Действие |
|---------|---------|----------|
| `ERR pattern_add` | >1024 слотов | Укоротить паттерн или уменьшить repeat delays |
| Prep warning в логе GUI | Pi без `send_line INIT_STIM` | Обновить сервер `--backend usb` |
| Стим «залипает» после RUN | только raw OFF | Добавить `PATTERN_ADD_WRITE 42 0 1 0`; прошивка с auto-OFF |
| Нет импульсов на осцилле | нет INIT_STIM / токов R64/R96 | §1 prep + RAW токов в pattern_load |
| `measure_impedance_fast требует /dev/intan` | старый backend | §6 — USB-bridge для импеданса |

---

## 5. Импеданс: workflow

### 5.1. На STM32 (уже работает)

Команда прошивки:

```text
IMPEDANCE_MEASURE <ch> <scale_bits> <freq_hz> <samples_per_period> <periods> <flags>
```

| Параметр | GUI «Шкала» | Рекомендация |
|----------|-------------|--------------|
| `scale_bits=0` | 0.1 pF | высокий Z |
| `scale_bits=1` | 1 pF | **10 kΩ эталон** |
| `scale_bits=3` | 10 pF | низкий Z, большой ток |

Для **1 kHz** и 10 kΩ: **`samples_per_period=16`**, **`periods=128`**, **`flags=3`** (phase_safe + restore_regs).

Проверка с Mac/Pi напрямую:

```bash
python3 tools/usb_intan_cmd.py INIT_RECORD 610 --no-reset
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 2 1 1000 16 128 3" --no-reset --timeout-ms 180000
```

Валидный ответ: `overruns=0`, `spi_errors=0`, `clipped=0`.

Пример (ch2, 10 kΩ на GND): **Z ≈ 9.8 kΩ**, `V_amp ≈ 38 µV`.

### 5.2. GUI (вкладка «Измерения»)

Сейчас GUI вызывает **`measure_impedance_fast`** по TCP — **не** `send_line("IMPEDANCE_MEASURE …")`.

Поля GUI → параметры STM32:

| Поле GUI | Маппинг |
|----------|---------|
| Канал 0–15 | `channel` |
| Частота Hz | `frequency` → `freq_hz` |
| Шкала 0.1/1/10 pF | `scale` → `scale_bits` |
| Усреднения | `num_averages` (на USB пока 1 проход STM32; Pi может повторить команду) |
| Авто-шкала | `auto_scale`: перебор scale на Pi |

**Разрыв:** без USB-backend на Pi кнопка «Измерить импеданс» падает с `measure_impedance_fast требует backend /dev/intan`.

### 5.3. Что должен делать Pi (USB-backend)

Псевдокод `measure_impedance_fast`:

```python
SCALE = {"0.1 pF": 0, "1 pF": 1, "10 pF": 3}
C_FARAD = {"0.1 pF": 0.1e-12, "1 pF": 1e-12, "10 pF": 10e-12}

def measure_impedance_fast_usb(channel, frequency, scale_str, ...):
    scale_bits = SCALE[scale_str]
    spp = 16 if frequency >= 1000 else 32   # см. intan_impedance_guide.md
    periods = 128
    flags = 3

    line = f"IMPEDANCE_MEASURE {channel} {scale_bits} {int(frequency)} {spp} {periods} {flags}"
    text = usb_run_text(line, timeout_ms=180_000)   # tools/usb_intan_lib.run_text_command
    fields = parse_ok_impedance(text)               # sin_accum, cos_accum, sample_count, overruns, ...

    if fields["overruns"] != 0 or fields["spi_errors"] != 0:
        raise RuntimeError(f"invalid measurement: {text}")

    z_ohm, v_amp_uv, phase_deg = compute_z_from_accumulators(
        fields["sin_accum"], fields["cos_accum"],
        fields["sample_count"], frequency, C_FARAD[scale_str], spp,
    )
    # Алгоритм: intan_tcp_server._compute_impedance_metrics_from_accumulators

    return {
        "status": "ok",
        "impedance_ohm": z_ohm,
        "v_amp_uv": v_amp_uv,
        "phase_deg": phase_deg,
        "likely_floating": v_amp_uv < 15.0,
        ...
    }
```

Перед измерением: **`STOP`** или убедиться, что **нет активного `SPI_STREAM_*`** — иначе `ERR busy`.

После серии: `READ 2` / `READ 3` → `0x0000` / `0x0080`.

### 5.4. Альтернатива: GUI через `send_line`

Можно доработать GUI, чтобы звать:

```python
resp = client.send_line("IMPEDANCE_MEASURE 2 1 1000 16 128 3", timeout=180)
# парсинг sin_accum/cos_accum локально в GUI
```

Тогда Pi достаточно проброса `send_line` без отдельного `measure_impedance_fast`. Пересчёт Z — тот же, что в §5.3.

### 5.5. Диагностика импеданса

| Поле ответа | Норма | Проблема |
|-------------|-------|----------|
| `overruns` | 0 | loop не успевает → ↓ freq или ↓ samples_per_period |
| `clipped` | 0 | насыщение ADC → канал в воздухе или слишком большой scale |
| `spi_errors` | 0 | SPI/CS |
| `v_amp_uv` | > 15 (10 kΩ, 1 pF) | < 15 → `likely_floating` |
| `actual_freq_millihz` | ≈ freq×1000 | сильный drift → проверить SYSCLK |

---

## 6. Реализация USB-backend на Pi (эскиз)

Минимальный модуль поверх `tools/usb_intan_lib.py` (скопировать `tools/` на Pi или установить пакет):

```python
# intan_usb_backend.py — эскиз для Orange Pi

import re
from usb_intan_lib import open_device, run_text_command, close_device

class Stm32UsbBridge:
    def __init__(self):
        self.dev, self.ifn = open_device(reset=False)

    def send_line(self, line: str, timeout_ms: int = 15000) -> str:
        return run_text_command(self.dev, line.strip(), timeout_ms=timeout_ms)

    def pattern_load(self, commands: list[str]) -> int:
        self.send_line("PATTERN_CLEAR", timeout_ms=5000)
        for cmd in commands:
            reply = self.send_line(cmd, timeout_ms=15000)
            if reply.startswith("ERR"):
                raise RuntimeError(reply)
        return len(commands)

    def pattern_run(self, repeat_count: int) -> None:
        timeout_ms = max(60_000, repeat_count * 500)
        reply = self.send_line(f"PATTERN_RUN {repeat_count}", timeout_ms=timeout_ms)
        if reply.startswith("ERR"):
            raise RuntimeError(reply)

    def close(self):
        close_device(self.dev, self.ifn)


def parse_impedance_reply(text: str) -> dict:
    """OK IMPEDANCE channel=2 sin_accum=... cos_accum=... sample_count=2048 overruns=0 ..."""
    out = {}
    for key in ("sin_accum", "cos_accum", "sample_count", "overruns", "spi_errors",
                "clipped", "actual_freq_millihz", "samples_per_period"):
        m = re.search(rf"\b{key}=(\S+)", text)
        if m:
            out[key] = int(m.group(1))
    return out
```

Интеграция в TCP handler: при `--backend usb` подменить `self.controller.spi` на `Stm32UsbBridge`, реализовать:

- `send_line` → `bridge.send_line`
- `pattern_load` → `bridge.pattern_load`
- `pattern_run` → `bridge.pattern_run`
- `measure_impedance_fast` → §5.3

---

## 7. Запуск GUI на ПК

```bash
# ПК, каталог WeActSTM32H743:
python3 intan_gui_new.py
```

В GUI: **IP Orange Pi**, порт **9000**, «Подключиться».

Проверка:

1. **ID** / `read_register` 255 → chip=32  
2. **Паттерн:** «📄 Пример» → «Загрузить» → «Запустить»  
3. **Импеданс:** канал с 10 kΩ → «Измерить» (после USB-backend на Pi)

---

## 8. Чеклист интеграции

### Pi maintainer

- [ ] `intan_server.py --backend usb` с PyUSB к `0483:5741`
- [ ] `send_line`: проброс **всех** STM32-команд, включая `INIT_STIM`, `PATTERN_*`, `IMPEDANCE_MEASURE`
- [ ] `pattern_load`: `PATTERN_CLEAR` + список `PATTERN_ADD_*`
- [ ] `pattern_run`: длинный timeout
- [ ] `measure_impedance_fast`: `IMPEDANCE_MEASURE` + пересчёт Z (§5.3)
- [ ] Перед impedance/stream: `STOP` если нужно

### GUI user

- [ ] Pi server запущен, firewall открыт :9000
- [ ] `PING` / ID в GUI OK
- [ ] Паттерн: prep §1 в логе без warning
- [ ] Импеданс: `overruns=0` в логе сервера

---

## 9. Связанные файлы

| Файл | Содержание |
|------|------------|
| `intan_gui_new.py` | GUI, генерация паттернов, TCP-клиент |
| `tools/usb_intan_lib.py` | PyUSB, `run_text_command` |
| `tools/usb_intan_cmd.py` | CLI для отладки с хоста/Pi |
| `Core/Src/intan_pattern.c` | Очередь паттерна на MCU |
| `Core/Src/intan_spi.c` | `IMPEDANCE_MEASURE`, SPI/CS |
| `Core/Src/usb_stream_service.c` | Разбор USB-команд |
| `intan_stim_pattern_guide.md` | Детали R42/R44, RAW, bench |
| `intan_impedance_guide.md` | Zcheck, flags, 10 kΩ процедура |
