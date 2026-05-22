# RHS2116 Operational Rules

Этот документ фиксирует инварианты реализации RHS2116 для `stimulation`, `recording`, `impedance` и `phase-safe`.

## Базовые инварианты

- `R0` вычисляется только от реальной общей частоты ADC: `sample_rate_per_channel * channel_count`.
- `R1` задается профилем режима. Для `phase-safe` нужно явно очищать `absmode` (bit 5), `DSPen` (bit 4) и `DSP cutoff` (bits 3:0), затем обязательно восстанавливать исходный `R1`.
- `R2/R3` принадлежат только Zcheck. После любого impedance/phase измерения обязателен возврат в `R2=0x0000`, `R3=0x0080`.
- `R32/R33` не являются per-channel mask. Это глобальный unlock stimulation: `0xAAAA` и `0x00FF`. Во всех остальных режимах оба регистра должны быть `0x0000`.
- `R38` всегда держать `0xFFFF`.
- `R9` не использовать без прямой ссылки на даташит RHS2116.

## Triggered Register Policy

- Triggered-регистры: `R10`, `R12`, `R42`, `R44`, `R46`, `R48`, `R64-R79`, `R96-R111`.
- Политика одна для всех путей: сначала `shadow write` с `U=0`, затем один явный `commit` через запись в `R42` с `U=1`.
- Не смешивать старый `spidev` pipeline mindset с `/dev/intan` driver path. Для driver path не использовать допущения о возврате сырых ответов из `transfer()`.

## Mode Ownership

- `recording` владеет трактом усилителей и не должен silently включать stimulation unlock.
- `stimulation` перед подачей импульсов обязан иметь `R32/R33 = 0xAAAA/0x00FF`, корректные `R34/R35`, и коммитнутые triggered-регистры.
- `impedance/phase` перед стартом обязаны выключить stimulation и charge recovery, сделать safe-state, затем восстановить снимок критичных регистров после завершения.

## UDP Recording Contract

- Текущий бинарный формат пакета: little-endian `v2`: `[ver=2][sample_count:u32] + sample*`.
- Один `sample`: `timestamp:f64`, `pipeline_skip:u16`, `ch_count:u16`, `ch_list:u8[ch_count]`, `raw_count:u16`, `raw_values:u16[raw_count]`.
- GUI должен иметь один reader сокета и один parser path для этого формата.

## Проверка после правок

- Проверять переходы `recording -> impedance -> stimulation -> recording`.
- После правок в GUI/UDP/TCP/driver сверять, что контракт регистров совпадает с `services/server/rhs2116_profiles.py`.
