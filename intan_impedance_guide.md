# Гайд по измерению импеданса RHS2116 на STM32

Этот гайд описывает текущую команду `IMPEDANCE_MEASURE` в прошивке STM32H743 и практический порядок проверки электрода или тестового резистора на канале RHS2116.

## Главное правило SPI/CS

Для RHS2116 каждая команда должна быть отдельной 32-битной SPI-транзакцией:

```text
CS high idle -> CS low -> 32-bit word -> CS high
```

Это относится и к `WRITE Reg3`, и к `CONVERT(channel)`, и к dummy-словам для pipeline. Нельзя отправлять несколько 32-битных команд одним непрерывным SPI block-transfer без гарантированного подъёма CS между словами.

Текущий `IMPEDANCE_MEASURE` использует надёжный paced path: на каждый sample — полные 3-slot `Intan_WriteReg(3)` и `Intan_Convert(channel)` (как в даташите), DWT-ритм по целевой частоте, 20 ms settle после enable Zcheck, flush pipeline тремя `CONVERT` перед серией. CS поднимается между каждым 32-битным словом внутри этих вызовов.

## USB-команда

```text
IMPEDANCE_MEASURE <channel> <scale_bits> <freq_hz> <samples_per_period> <periods> <flags>
```

Параметры:

- `channel`: канал RHS2116, `0..15`.
- `scale_bits`: Zcheck capacitor scale: `0` = `0.1 pF`, `1` = `1 pF`, `3` = `10 pF`.
- `freq_hz`: частота тестового синуса, обычно `100`, `300` или `1000`.
- `samples_per_period`: число точек синуса на период. Для **`1 kHz` используйте `16`** (на STM32 с 3-slot SPI надёжнее, чем `8`).
- `periods`: число периодов усреднения.
- `flags`: bit0 `phase_safe`, bit1 `restore_regs`.

Практическая команда для тестового резистора `10 kOhm` на канале 0:

```bash
python3 tools/usb_intan_cmd.py INIT_RECORD 610 --no-reset
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 0 1 1000 16 128 3" --no-reset
```

(`samples_per_period=8` на `1 kHz` даёт заниженный Z из‑за budget SPI; для быстрой проверки допустимо, для калибровки — `16`.)

После измерения проверьте safe-state:

```bash
python3 tools/usb_intan_cmd.py READ 2 --no-reset
python3 tools/usb_intan_cmd.py READ 3 --no-reset
```

Ожидаемо:

```text
OK READ reg=2 value=0x0000
OK READ reg=3 value=0x0080
```

## Что делает STM32

Перед измерением прошивка:

- проверяет, что Intan SPI готов и stream не активен;
- сохраняет регистры `1, 2, 3, 32, 33, 42, 44, 46, 48`;
- переводит Zcheck и stimulation-регистры в safe-state;
- при `phase_safe` временно чистит в `Reg1` биты `absmode`, `DSPen` и cutoff;
- включает Zcheck через `Reg2 = (channel << 8) | (1 << 6) | (scale_bits << 3) | 1`;
- держит `Reg3 = 0x0080` перед стартом.

Перед стартом серии:

- `WRITE Reg2 0x0040` — включение Zcheck DAC power;
- `WRITE Reg3 0x0080` — нейтраль;
- `WRITE Reg2` с каналом/scale/enable;
- `WRITE Reg3 0x0080`;
- пауза **20 ms** (стабилизация DAC и переключателей);
- три `CONVERT(channel)` для flush SPI pipeline.

Во время измерения на каждый sample:

```text
WRITE Reg3 = sine_value   (3 SPI slot через Intan_WriteReg)
~2 us analog settle
CONVERT channel           (3 SPI slot, ADC через Intan_Convert)
```

Pacing: интервал отсчитывается от завершения `CONVERT` до следующего `WRITE Reg3`.
Перед серией: `CONVERT H=1`, затем три прогона `WRITE Reg3` + `CONVERT` для синхронизации pipeline.

Ответ ADC — из `Intan_Convert`, без ручной индексации pipeline. STM32 центрирует ADC-код как `adc - 32768` и накапливает zero-mean basis (orthogonal sin/cos для subsampled INTAN_SINE64):

```text
sin_accum += centered_adc * sin_basis
cos_accum += centered_adc * cos_basis
```

После измерения прошивка выключает Zcheck и принудительно возвращает:

```text
Reg2 = 0x0000
Reg3 = 0x0080
```

Если выставлен `restore_regs`, сохранённые регистры восстанавливаются, но затем `Reg2/Reg3` всё равно возвращаются в impedance safe-state.

## Ответ команды

Пример ответа:

```text
OK IMPEDANCE channel=0 scale=1 freq_hz=1000 actual_freq_millihz=1000000 samples_per_period=8 periods=128 sample_count=1024 sin_accum=... cos_accum=... adc_min=... adc_max=... adc_mean_x1000=... clipped=0 overruns=0 spi_errors=0 averages=1 p0_sin=... p0_cos=...
```

Поля для проверки качества:

- `actual_freq_millihz`: фактическая частота тестового синуса в mHz.
- `sample_count`: число накопленных samples.
- `sin_accum`, `cos_accum`: проекции ADC на синус/косинус.
- `adc_min`, `adc_max`, `adc_mean_x1000`: диапазон и среднее ADC.
- `clipped`: число samples с ADC `0` или `65535`; должно быть `0`.
- `overruns`: число опозданий timed loop; для валидного измерения должно быть `0`.
- `spi_errors`: SPI ошибки; должно быть `0`.

Если `overruns != 0`, измерение нельзя использовать для точной оценки импеданса. Уменьшите `samples_per_period`, частоту или число команд в loop.

## Пересчёт в Ohm

Zcheck DAC использует 8-битный синус `INTAN_SINE64` вокруг кода `128`. Для максимальной амплитуды:

```text
Vdac_peak ~= 0.6125 V
I_peak = 2 * pi * freq_hz * Cscale * Vdac_peak
Z_ohm = Velec_peak / I_peak
```

Для AC high-gain RHS2116:

```text
Velec_uV = 0.195 * ADC_code_amplitude
```

При `freq_hz = 1000` и полном DAC swing ориентировочные токи:

- `scale_bits=0`, `0.1 pF`: `~0.38 nA peak`.
- `scale_bits=1`, `1 pF`: `~3.8 nA peak`.
- `scale_bits=3`, `10 pF`: `~38 nA peak`.

Для резистора `10 kOhm` ожидаемая амплитуда напряжения:

- `scale_bits=1`: около `38 uV peak`.
- `scale_bits=3`: около `380 uV peak`.

Прошивка считает амплитуду из `sin_accum/cos_accum` через zero-mean basis (см. `IMPEDANCE_MEASURE` в `Core/Src/usb_stream_service.c`).

## Проверенный режим для 10 kOhm

На текущей плате с резистором `10 kOhm` на канале 0 валидный практический режим:

```bash
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 0 1 1000 16 128 3" --no-reset
```

Наблюдавшийся результат:

```text
scale_bits=1
freq_hz=1000
samples_per_period=16
periods=128
overruns=0
spi_errors=0
clipped=0
median impedance ~= 8.5 kOhm
```

Это достаточно близко к тестовому резистору `10 kOhm` для первичной проверки тракта. Разброс по отдельным запускам возможен, поэтому для GUI лучше считать median по нескольким измерениям.

## Ограничения текущей реализации

Каждый sample — 6 SPI slot (WriteReg + Convert). На высоких частотах и большом `samples_per_period` loop может не успевать; признак — `overruns > 0`. Уменьшите `freq_hz` или `samples_per_period`.

Для `1 kHz` не используйте `64 samples_per_period`: loop не успевает. Признак проблемы:

```text
overruns > 0
```

Рабочие ориентиры:

- `1000 Hz`, `8 samples_per_period`: валидно, `overruns=0`.
- `300 Hz`, `32 samples_per_period`: валидно по таймингу, но результат зависит от scale и аналогового состояния.
- `100 Hz`, `64 samples_per_period`: валидно по таймингу, но сигнал на малых токах может быть слабым.

`scale_bits=3` даёт больший тестовый ток и лучший SNR, но на текущей проверке с `10 kOhm` давал заниженную оценку около `5..6 kOhm`. Для контрольного резистора `10 kOhm` сейчас используйте `scale_bits=1`.

## Рекомендуемая процедура проверки

1. Подключить резистор `10 kOhm` к проверяемому каналу согласно схеме платы.
2. Инициализировать recording profile:

```bash
python3 tools/usb_intan_cmd.py INIT_RECORD 610 --no-reset
python3 tools/usb_intan_cmd.py CLEAR_ADC --no-reset
```

3. Выполнить серию измерений:

```bash
python3 tools/usb_intan_cmd.py "IMPEDANCE_MEASURE 0 1 1000 16 128 3" --no-reset
```

4. Проверить, что в каждом ответе:

```text
overruns=0
spi_errors=0
clipped=0
actual_freq_millihz=1000000
```

5. Пересчитать `sin_accum/cos_accum` в Ohm и взять median по нескольким запускам.
6. После серии проверить safe-state:

```bash
python3 tools/usb_intan_cmd.py READ 2 --no-reset
python3 tools/usb_intan_cmd.py READ 3 --no-reset
```

## Диагностика

Если результат сильно отличается от ожидаемого:

- Проверьте `overruns`; при ненуле V измерение невалидно.
- Проверьте `clipped`; при clipping уменьшите тестовый ток (`scale_bits`) или проверьте bias/подключение.
- Проверьте `adc_min/adc_max`; коды не должны постоянно сидеть около `0` или `65535`.
- Проверьте `Reg2/Reg3` после измерения; они должны вернуться в `0x0000/0x0080`.
- Повторите `INIT_RECORD 610`, если перед этим запускались stimulation patterns.
- Не используйте DMA/block SPI для Zcheck, если CS не поднимается между каждым 32-битным словом.

## Что улучшить дальше

- Добавить параметр DAC amplitude, чтобы не всегда использовать полный swing `INTAN_SINE64`.
- Вернуть быстрый режим через специализированный timer/DMA только после аппаратной проверки, что CS гарантированно поднимается между каждым 32-битным word и SPI frame действительно разделён для RHS2116.
- Добавить host-утилиту, которая запускает серию `IMPEDANCE_MEASURE`, считает median/std и сразу печатает `impedance_ohm`.
- Откалибровать `scale_bits=3` на известных резисторах `1 kOhm`, `10 kOhm`, `100 kOhm`.
