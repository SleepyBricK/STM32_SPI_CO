# MCP: DSLogic + STM32 Intan

MCP-сервер для Cursor: USB-статус DSLogic U2Basic, stream STM32, разбор CSV из DSView.

## Почему свой MCP

| Репозиторий | U2Basic (0x0029) |
|-------------|------------------|
| [mcp-sigrok](https://github.com/daedalus/mcp-sigrok) | Нет |
| [logic-analyzer-mcp](https://github.com/sandraschi/logic-analyzer-mcp) | Нет |
| [agent-dsviewer-logic-analyzer](https://github.com/felixfinal/agent-dsviewer-logic-analyzer) | Да (libsigrok4DSL + dslogic-cli) |
| [logicanalyzer-mcp](https://github.com/DatanoiseTV/logicanalyzer-mcp) | Да (сборка Go + submodule) |

**U2Basic** не в libsigrok 0.5.x → sigrok-based MCP не захватывают. Этот MCP:

- видит LA на USB (pyusb);
- гоняет `SPI_STREAM_REAL` на STM32 с drain;
- считает импульсы NSS в CSV из DSView;
- подсказывает внешние GitHub MCP (`github_mcp_survey`).

Полный захват U2Basic из терминала: собрать [agent-dsviewer-logic-analyzer](https://github.com/felixfinal/agent-dsviewer-logic-analyzer) (Linux + `libsigrok4DSL.so` из DSView).

## Установка

```bash
pip3 install -r tools/mcp_dslogic_intan/requirements.txt
chmod +x tools/mcp_dslogic_intan/run.sh
```

## Cursor

Файл `.cursor/mcp.json` в корне репозитория. Перезапустите Cursor → Settings → MCP → `dslogic-intan`.

## Tools

| Tool | Назначение |
|------|------------|
| `dslogic_usb_scan` | USB: DSLogic / STM32 5741 |
| `dslogic_environment` | DSView, sigrok, рекомендации |
| `github_mcp_survey` | Ссылки на GitHub MCP |
| `stm32_intan_stream` | Stream + bulk drain для LA |
| `analyze_dsview_csv` | NSS pulses в экспорте DSView |

## Провода LA

- D0 = SCK (PA9), D1 = MISO, D2 = MOSI, D3 = NSS (PA11)
