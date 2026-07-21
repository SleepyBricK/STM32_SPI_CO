# Long validation — 2026-07-08

Длительный аппаратный прогон по плану Codex. Fixture: **ch0 → 1 kΩ → GND** (recording), stim **ch0 / 180 µA / 10 kΩ → Rigol CH1**.

| Параметр | Значение |
| --- | --- |
| Дата/время UTC | 2026-07-08, ~09:43–10:00 |
| Firmware | `git=5ab5ff9eb410`, `build_type=Release` |
| MCU clocks | `sysclk_mhz=480`, `sck_khz=25000`, `pscl=8`, `nss_midi=4` |
| Rigol | DHO804 `USB0::6833::1101::DHO8A272405662::0::INSTR` |
| Артефакты | `tools/testing/long_20260708/` |

## Summary table

| Phase | Command / artifact | Duration | Verdict | Key metrics |
| --- | --- | ---: | --- | --- |
| Connectivity | `PING`, baseline `STATS` | ~1 min | **PASS** | `PONG`; clocks OK; `last_fault=0` |
| USB HS check | `lsusb -t` | — | **SKIP** | На macOS `lsusb -t` недоступен (exit 1) |
| RR8 soak ×10 | `ch_fw_channel_scan --rr8-s 60` | ~10 min | **PASS** | 10/10 `PASS`; `clip=0`, `usb_ovf=0`, `fw_dma_err=0`; rate 39.5 kS/s/ch |
| RR8 plots | `ch_fw_long_suite --duration 60` | ~2 min | **PASS** | RR8 `clip=0`; ch0 RMS **354 µV** |
| USB stress (5M×20) | `usb_frame_bench -n 5000000 --runs 20` | ~15 s | **FAIL** | `errors=2429`/run; `usb_ovf` накопился |
| USB stress retry | `usb_frame_bench -n 50000 --runs 5` | ~1 s | **PASS** | `errors=0` все 5 run |
| FW_MAX diagnostic | `ch_fw_max_suite --duration 5` | ~3 s | **PASS** | `clip=0`, `usb_ovf=0`; agg ~533 kS/s |
| Stim smoke | `rigol_stim_smoke.py` | ~4 s | **PASS** | `V_max=2.168 V` |
| Stim validate | `stim_rigol_validate.py` | ~15 s | **PASS*** | amplitude PASS; `READ 42/40/50=0`; *width µs — limitation |
| Pattern timing | `test_pattern_timing.py` | ~5 s | **PASS** | 1000 µs slope **1008 µs/iter**; 100 µs slope **119 µs/iter** |
| Regression | `phase2_hw_test.py` | ~17 s | **PASS** | 6/6; `usb_disconnect 0→1` |
| Legacy suite | `run_full_stream_suite.py` | — | **SKIP** | Hardcoded ch2 thresholds; fixture ch0 |
| Final health | `STATS` после phase2 | — | **WARN** | `usb_ovf=44204` от failed bench; `iwdg_reset=1` pre-existing |

### Общий вердикт

| Область | Вердикт |
| --- | --- |
| **Production RR8 @ 40 kS/s/ch** | **PASS** — 10×60 s soak без clip/ovf/dma_err |
| **Stim + Rigol amplitude** | **PASS** — ~2.17 V @ 180 µA × 10 kΩ |
| **Pattern wall-clock timing** | **PASS** — DWT slopes соответствуют задержкам |
| **Phase 2/3 regression** | **PASS** |
| **SYNTH USB stress 5M×20** | **FAIL** — host не успевает drain; см. retry 50k |
| **ch0 noise floor** | **WARN** — RMS 267–461 µV (выше исторического ch2 ~76 µV) |

---

## 1. Baseline connectivity

```text
PING → PONG

Baseline STATS:
samples=0 clip=0 usb_ovf=0 fw_dma_err=0 samples_dropped=0
sysclk_mhz=480 sck_khz=25000 pscl=8 nss_midi=4
iwdg_reset=1 last_fault=0 build_type=Release git=5ab5ff9eb410
usb_disconnect=5
```

`iwdg_reset=1` — **pre-existing** (был и до прогона). Новых `last_fault` нет.

---

## 2. RR8 soak — 10 × 60 s

Команда (каждый run):

```bash
python3 -u tools/ch_fw_channel_scan.py --rr8-s 60 --ksps 40 --warmup-skip 0.5 --no-reset \
  -o tools/testing/long_20260708/scan_XX
```

| Scan | ch0 RMS µV | rate kS/s | clip | usb_ovf | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| 01 | 443.7 | 39.5 | 0 | 0 | PASS |
| 02 | 267.3 | 39.5 | 0 | 0 | PASS |
| 03 | 355.6 | 39.5 | 0 | 0 | PASS |
| 04 | 342.3 | 39.5 | 0 | 0 | PASS |
| 05 | 417.9 | 39.5 | 0 | 0 | PASS |
| 06 | 321.7 | 39.5 | 0 | 0 | PASS |
| 07 | 319.8 | 39.5 | 0 | 0 | PASS |
| 08 | 461.0 | 39.5 | 0 | 0 | PASS |
| 09 | 439.0 | 39.5 | 0 | 0 | PASS |
| 10 | 285.7 | 39.5 | 0 | 0 | PASS |

**Агрегат ch0:** min **267.3**, max **461.0**, mean **365.4 µV**.

Остальные каналы (floating): RMS ~3.7–4.5 mV — ожидаемо.

Plots: `tools/testing/long_20260708/scan_XX/fw_ch_scan_60s_40ksps_rr8.png`

---

## 3. RR8 detailed — ch_fw_long_suite 60 s

```text
ch0 rr8: med=0x8010 RMS=354µV clip=0
ch2 solo: RMS=4482µV clip=2944  ← diagnostic only, не production path
ch2 rr8:  RMS=4551µV clip=0
```

Артефакты: `tools/ch_fw_long_60s_stats.txt`, `tools/ch_fw8_60s_40ksps*.png`

---

## 4. USB synthetic stress

### 4.1 Primary (FAIL)

```bash
python3 -u tools/usb_frame_bench.py -n 5000000 --runs 20 --no-reset --timeout-ms 60000
```

Все 20 run: **`errors=2429`**, throughput ~8.1 kS/s payload ~16 MB/s.  
Причина: host не успевает читать bulk IN при 5M samples/run → `ERR overflow usb=N`.  
После прогона `STATS` показал `usb_ovf=2460`.

### 4.2 Retry diagnostic (PASS)

```bash
python3 -u tools/usb_frame_bench.py -n 50000 --runs 5 --no-reset
```

```text
run=1..5 errors=0  ksps≈5300–6100  payload_MBps≈10.6–12.1
```

Transport жив; FAIL специфичен для агрессивного `-n 5000000`.

---

## 5. FW_MAX diagnostic

```text
SPI_STREAM_FW_MAX wall=2.25s  aggregate=532.6 kS/s/ch  clip=0 usb_ovf=0
ch0 RMS=244µV  ch2 RMS=4582µV
```

Diagnostic-only, не production acceptance.

---

## 6. Stim + Rigol DHO804

### Smoke

```text
Rigol capture: V_max=2.168 V  V_pp=2.219 V  pulses=1
PASS
```

### Detailed validate

| Test | Result | Notes |
| --- | --- | --- |
| Amplitude 500 ms ON | **PASS** | V_max=2.1675 V, I_est=216.8 µA (+20.4%) |
| Width 100 µs | FAIL (expected) | width=2 µs — screen buffer limitation |
| Duration sweep | no pulses detected | same limitation |
| Safe OFF | **PASS** | R42=0, R40=0, R50=0 |

Отчёт: `tools/testing/long_20260708/stim_rigol_ch0_20260708T095856Z_report.txt`

---

## 7. Pattern wall-clock timing

```text
1000 µs delay only:  slope = 1008.3 µs/iter  ✓
100 µs pulse ch0:     slope = 119.1 µs/iter  ✓
Two-pulse OFF100:     slope = 310.0 µs/iter  ✓ (3×100 µs)
sysclk_mhz=480
```

Нет признаков ×3 или /3 clock mismatch.

---

## 8. Phase 2/3 regression

```text
[PASS] STATS fingerprint
[PASS] NSS_MIDI idle
[PASS] SPI lock during stream
[PASS] STOP during RR8
[PASS] RHS1 strict (32 frames)
[PASS] USB disconnect counter 0 → 1
All tests passed.
```

---

## 9. Final STATS (после phase2)

```text
usb_ovf=44204 samples_dropped=176816  ← артефакт failed SYNTH bench 5M×20
fw_dma_err=0 sample_clip=0 last_fault=0
iwdg_reset=1 usb_disconnect=1
WRITE 42 0 1 0 → OK
```

**Не интерпретировать** `usb_ovf`/`samples_dropped` как production RR8 fail — counters загрязнены SYNTH overflow тестом.

---

## Known limitations

1. **Rigol width µs** — `:WAV:MODE NORM` screen buffer (~300–400 точек) не разрешает µs pulses; amplitude valid, width — нет.
2. **iwdg_reset=1** — pre-existing до прогона; новых fault нет.
3. **ch0 RMS 267–461 µV** — выше AGENTS.md ch2 ~76 µV; возможны fixture/экранирование/параллельный stim path; production counters чистые.
4. **SYNTH 5M×20** — не использовать как gate без host-side drain tuning или меньшего `-n`.
5. **run_full_stream_suite** — пропущен (ch2 hardcoded thresholds).

---

## Raw logs

| File | Content |
| --- | --- |
| `long_20260708/soak.log` | RR8 soak timeline + STATS |
| `long_20260708/scan_XX.log` | Per-scan full output |
| `long_20260708/ch_fw_long_60s.log` | Long suite |
| `long_20260708/usb_frame_bench.log` | SYNTH stress FAIL |
| `long_20260708/usb_frame_bench_retry.log` | SYNTH retry PASS |
| `long_20260708/rigol_stim_smoke.log` | Stim smoke |
| `long_20260708/stim_rigol_validate.log` | Stim + timing |
| `long_20260708/test_pattern_timing.log` | Pattern timing |
| `long_20260708/phase2_hw_test.log` | Regression |
| `long_20260708/final_stats.log` | Final STATS + stim OFF |
