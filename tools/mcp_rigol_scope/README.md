# MCP: Rigol DHO804 + Intan

MCP-сервер для Cursor: управление Rigol по USB/SCPI (PyVISA), захват waveform, измерение импульсов stim.

## Подключённое устройство

| Параметр | Значение |
| --- | --- |
| Модель | **DHO804** |
| Serial | DHO8A272405662 |
| Firmware | 00.01.05 |
| VISA | `USB0::6833::1101::DHO8A272405662::0::INSTR` |

Закройте **UltraScope / Rigol PC software**, если VISA занят другим процессом.

## Установка

```bash
pip3 install -r tools/mcp_rigol_scope/requirements.txt
chmod +x tools/mcp_rigol_scope/run.sh
```

## Cursor

Создайте `.cursor/mcp.json` в корне репозитория (файл в `.gitignore`):

```json
{
  "mcpServers": {
    "rigol-scope": {
      "command": "/Users/warforterritory/STM32Cube/WeActSTM32H743/tools/mcp_rigol_scope/run.sh"
    }
  }
}
```

Перезапустите Cursor → Settings → MCP → включите **rigol-scope**.

## Tools

| Tool | Назначение |
| --- | --- |
| `rigol_scan` | VISA resources + авто-поиск Rigol |
| `rigol_status` | timebase, trigger, каналы |
| `rigol_configure` | scale, coupling, trigger |
| `rigol_capture` | waveform stats, опционально CSV |
| `rigol_measure_pulses` | ширины HIGH-импульсов (µs) |
| `rigol_run` / `rigol_stop` | RUN/STOP |
| `rigol_intan_wiring` | подсказка по проводам stim |

## DHO804 capture sequence

Для stim-паттернов не опрашивайте scope циклом `STOP/RUN`. Надёжная последовательность:

1. `configure_stim()` / `rigol_configure`: channel/timebase/trigger AUTO, уровень через DHO804 `:TRIG:LEV:CHn`.
2. `:RUN` (не `:SING` — на DHO804 `:TRIG:STAT?` всегда `STOP` и не годится для sync).
3. Запустить `PATTERN_RUN` на STM32 в отдельном потоке.
4. Пока паттерн идёт, периодически читать `:WAV:MODE NORM` и брать кадр с max(V).
5. `:STOP`, вернуть лучший waveform.

В Python для этого используйте `measure_pulses_synced(resource, run_fn, ...)` из `tools/mcp_rigol_scope/lib.py`.

## Intan stim (вместо Moku)

```
STM32 ── USB ── Intan RHS2116 ── elec2 ── 10 kΩ ── GND
Rigol CH1 ── на один конец 10 kΩ (напряжение на нагрузке)
```

180 µA × 10 kΩ ≈ **1.8 V**. Скрипты `pattern_scope_test.py` / `pattern_sweep_scope.py` используют Moku — те же измерения можно делать через MCP `rigol_measure_pulses` после `PATTERN_RUN`.

## CLI smoke test

```bash
python3 - <<'PY'
import json, sys
sys.path.insert(0, "tools/mcp_rigol_scope")
from lib import scan_rigol, scope_status, capture_summary
print(json.dumps(scan_rigol(), indent=2))
print(json.dumps(scope_status(), indent=2))
print(json.dumps(capture_summary(points=500), indent=2))
PY
```
