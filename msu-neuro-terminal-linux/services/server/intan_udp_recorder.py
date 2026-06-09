#!/usr/bin/env python3
"""
UDP-сервер для регистрации данных с Intan RHS2116.

Читает данные ADC со всех 16 каналов и отправляет их по UDP клиентам.
Использует CONVERT команды для получения данных с усилителей.

ОПТИМИЗАЦИИ ПРОИЗВОДИТЕЛЬНОСТИ:
- DMA используется автоматически на уровне драйвера SPI в ядре Linux
- Используется CONVERT(63) для автоматического переключения каналов (максимальная скорость)
- Оптимизировано формирование UDP пакетов: предварительное выделение памяти, упаковка несколькими полями одним вызовом
- Буферизация samples для упаковки нескольких точек в один пакет (оптимальный размер ~1400 байт)
- Убраны все логи в циклах регистрации для минимальных накладных расходов
- Предварительно вычисленные команды SPI для уменьшения вычислений в цикле

Запуск:
  python3 intan_udp_recorder.py --udp-port 9001 --channels 0-15 --sample-rate 40000
"""

import argparse
import queue
import socket
import struct
import threading
import time
import sys
import os
import math
from collections import namedtuple

from stimulate_channel0 import _is_usb_backend, convert_intan
from intan_rhs1 import (
    RR8_CHANNELS,
    RHS1_FLAG_CHANNEL_TAG,
    Rhs1ChannelRouter,
    pack_rr8_multichannel,
    parse_rhs1_header,
    rhs1_raw_payload_bytes,
)
from intan_usb_transport import V2_STREAM_RELOAD_SAMPLES

# USB STREAM(4096): один batch = сырые bytes с USB (без struct.unpack).
StreamBatch = namedtuple(
    "StreamBatch",
    (
        "timestamp",
        "payload",
        "channels",
        "pipeline_skip",
        "hw_counter",
        "capture_ts_ns",
        "adc_rate_hz",
        "stream_mux_count",
    ),
    defaults=(0, 0, 350_000, 1),
)

INTAN_STREAM_MAGIC = 0x334E5449
INTAN_STREAM_VERSION = 3
V3_HEADER_SIZE = 44
V3_CHANNEL_TEMPLATE = bytes(range(16))
INTAN_STREAM_V4_MAGIC = 0x344E5449
INTAN_STREAM_V4_VERSION = 4
V4_HEADER_SIZE = 16
INTAN_STREAM_V5_MAGIC = 0x354E5449
INTAN_STREAM_V5_VERSION = 5
V5_HEADER_SIZE = 20
INTAN_STREAM_V6_MAGIC = 0x364E5449
INTAN_STREAM_V6_VERSION = 6
V6_HEADER_SIZE = 32
USB_V2_FIRMWARE_ADC_KSPS = 350
USB_V2_FIRMWARE_ADC_HZ = USB_V2_FIRMWARE_ADC_KSPS * 1000
INTAN_UDP_WIFI = os.environ.get("INTAN_UDP_WIFI", "1") == "1"
INTAN_UDP_USE_V5 = os.environ.get("INTAN_UDP_V5", "0") == "1"
INTAN_UDP_USE_V6 = os.environ.get("INTAN_UDP_V6", "0") == "1"
USB_V2_YIELD_TARGET_BYTES = int(
    os.environ.get(
        "INTAN_UDP_YIELD_BYTES",
        "262144" if INTAN_UDP_WIFI else "65536",
    )
)
USB_V2_BURST_EXTRA_FRAMES = 31 if INTAN_UDP_WIFI else 15
UDP_SEND_QUEUE_SIZE = 4096 if INTAN_UDP_WIFI else 1024
UDP_SENDER_THREADS = 2 if INTAN_UDP_WIFI else 1
UDP_SENDER_DRAIN_BATCHES = 64 if INTAN_UDP_WIFI else 32
STREAM_BATCH_QUEUE_SIZE = 12 if INTAN_UDP_WIFI else 0
# Без фрагментации IP: 1500 - 20 IP - 8 UDP = 1472
MAX_UDP_DATAGRAM = 1472
MUX_FRAME_CHANNELS = 16


def _rhs1_stream_batch(
    frame,
    payload: bytes,
    channels: list,
    *,
    pipeline_skip: int = 0,
    wall_ts: float | None = None,
    capture_ts_ns: int | None = None,
) -> StreamBatch:
    """Метаданные RHS1 для честной привязки времени на host."""
    mux_count = frame.channel_count if frame.channel_count > 0 else max(1, len(channels))
    if capture_ts_ns is None:
        capture_ts_ns = time.time_ns() if INTAN_UDP_USE_V6 else 0
    return StreamBatch(
        wall_ts if wall_ts is not None else time.time(),
        payload,
        channels,
        pipeline_skip,
        frame.first_sample_counter,
        capture_ts_ns,
        USB_V2_FIRMWARE_ADC_HZ,
        mux_count,
    )


def _extract_mux63_payload(payload: bytes, channels: list) -> bytes:
    """
    STREAM(4096) с ch=63 даёт кадры по 16 каналов (mux).
    Вырезаем только запрошенные каналы в плотный буфер для v3.
    """
    nch = len(channels)
    if nch <= 0:
        return b""
    if nch == MUX_FRAME_CHANNELS and channels == list(range(MUX_FRAME_CHANNELS)):
        return payload

    frame_bytes_mux = MUX_FRAME_CHANNELS * 2
    n_frames = len(payload) // frame_bytes_mux
    if n_frames <= 0:
        return b""

    out = bytearray(n_frames * nch * 2)
    out_mv = memoryview(out)
    pay_mv = memoryview(payload)

    if nch == 1:
        ch = channels[0]
        for f in range(n_frames):
            src = f * frame_bytes_mux + ch * 2
            out_mv[f * 2 : f * 2 + 2] = pay_mv[src : src + 2]
        return bytes(out)

    start = channels[0]
    if channels == list(range(start, start + nch)):
        for f in range(n_frames):
            src = f * frame_bytes_mux + start * 2
            dst = f * nch * 2
            out_mv[dst : dst + nch * 2] = pay_mv[src : src + nch * 2]
        return bytes(out)

    for f in range(n_frames):
        frame_base = f * frame_bytes_mux
        dst_base = f * nch * 2
        for j, ch in enumerate(channels):
            src = frame_base + ch * 2
            dst = dst_base + j * 2
            out_mv[dst : dst + 2] = pay_mv[src : src + 2]
    return bytes(out)

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    # Fallback для систем без numpy (используем простые вычисления)
    NUMPY_AVAILABLE = False
    
    class np:
        @staticmethod
        def array(data):
            # Возвращаем список, но операции будут выполняться через встроенные функции
            return data if isinstance(data, list) else list(data)
        @staticmethod
        def sqrt(x):
            return math.sqrt(x)
        @staticmethod
        def mean(data):
            if isinstance(data, list):
                return sum(data) / len(data) if len(data) > 0 else 0
            return float(data)

from stimulate_channel0 import (
    GPIOController,
    GPIOError,
    SPIController,
    initialize_intan_chip,
    read_intan_register,
    write_intan_register,
    clear_adc,
    clear_compliance_monitor,
)


class IntanRecorder:
    """Класс для регистрации данных с Intan RHS2116"""

    def __init__(
        self,
        gpio_number=226,
        spi_device="/dev/spidev1.1",
        verbose=False,
        transport=None,
        backend="spi",
    ):
        self.gpio_number = gpio_number
        self.spi_device = spi_device
        self.verbose = verbose
        self.transport = transport
        self.backend = backend

        self.gpio = None
        self.spi = None
        self.initialized = False
        self._record_ksps = USB_V2_FIRMWARE_ADC_KSPS
        self._usb_stream8_ok = None
        self._usb_stream_range_ok = None
        self.recording = False
        self.lock = threading.Lock()
        
        # Автоматически экспортируем GPIO при инициализации
        self._ensure_gpio_exported()

    def _ensure_gpio_exported(self):
        """Убеждается, что GPIO экспортирован"""
        gpio_path = f"/sys/class/gpio/gpio{self.gpio_number}"
        if not os.path.exists(gpio_path):
            try:
                export_script = "/home/admin/export_gpio.sh"
                if os.path.exists(export_script):
                    import subprocess
                    subprocess.run(
                        ["sudo", export_script, str(self.gpio_number)],
                        capture_output=True,
                        timeout=2
                    )
            except Exception:
                pass

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def ensure_initialized(self):
        """Инициализирует USB или GPIO/SPI и чип Intan"""
        with self.lock:
            if self.initialized:
                return

            self._log("== Инициализация Intan для регистрации ==")

            try:
                if self.backend == "usb":
                    if self.transport is None:
                        raise RuntimeError("USB backend requires transport")
                    self.spi = self.transport
                    self.transport.verify_chip()
                    if self.transport.firmware_version() == "v2":
                        self._log(
                            "USB V2: INIT_RECORD выполняется прошивкой при старте *_REAL stream"
                        )
                    else:
                        self._log("Инициализация чипа для регистрации (USB)...")
                        self._initialize_for_recording(
                            verbose=self.verbose, adc_sampling_rate_ksps=USB_V2_FIRMWARE_ADC_KSPS
                        )
                else:
                    self.gpio = GPIOController(self.gpio_number, raise_exceptions=True)
                    self.gpio.set_direction("out")
                    self.gpio.set_value(1)
                    time.sleep(0.1)

                    if not os.path.exists(self.spi_device):
                        raise FileNotFoundError(
                            f"SPI устройство не найдено: {self.spi_device}"
                        )

                    self.spi = SPIController(
                        device=self.spi_device,
                        max_speed_hz=10000000,
                        mode=0,
                    )
                    self.spi.open()

                    chip_id = read_intan_register(self.spi, 255, verbose=False)
                    if chip_id != 32:
                        raise RuntimeError(
                            f"Неверный Chip ID: {chip_id} (ожидалось 32)"
                        )

                    self._log("Инициализация чипа для регистрации данных...")
                    self._initialize_for_recording(
                        verbose=self.verbose, adc_sampling_rate_ksps=USB_V2_FIRMWARE_ADC_KSPS
                    )

                self.initialized = True
                self._log("== Инициализация завершена ==")
            except Exception as e:
                self._log(f"❌ Ошибка инициализации: {e}")
                raise

    def _reinitialize_for_stimulation(self, verbose=False):
        """
        Полная переинициализация чипа для стимуляции после измерения импеданса.
        Аналогична _initialize_for_stimulation из IntanController.
        """
        spi = self.spi
        
        if verbose:
            self._log("      Полная переинициализация для стимуляции...")
        
        # 1. READ 255 U=0 M=0 - dummy команда
        read_intan_register(spi, 255, verbose=False)
        time.sleep(0.001)
        
        # 2. WRITE 32/33 0x0000 - отключить стимуляцию
        write_intan_register(spi, 32, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 33, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 3. Инициализация основных регистров
        write_intan_register(spi, 38, 0xFFFF, u_flag=0, m_flag=0, verbose=False)  # DC-coupled amplifiers
        time.sleep(0.001)
        clear_adc(spi, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 0, 0x00C5, u_flag=0, m_flag=0, verbose=False)  # ADC config
        time.sleep(0.001)
        write_intan_register(spi, 1, 0x051A, u_flag=0, m_flag=0, verbose=False)  # DSP filter
        time.sleep(0.001)
        write_intan_register(spi, 2, 0x0000, u_flag=0, m_flag=0, verbose=False)  # Zcheck выкл.
        time.sleep(0.001)
        write_intan_register(spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)  # нейтральное значение
        time.sleep(0.001)
        write_intan_register(spi, 4, 0x0016, u_flag=0, m_flag=0, verbose=False)  # Upper cutoff 7.5 kHz
        time.sleep(0.001)
        write_intan_register(spi, 5, 0x0017, u_flag=0, m_flag=0, verbose=False)  # Lower cutoff 5 Hz
        time.sleep(0.001)
        write_intan_register(spi, 6, 0x00A8, u_flag=0, m_flag=0, verbose=False)  # Lower cutoff 5 Hz
        time.sleep(0.001)
        write_intan_register(spi, 7, 0x000A, u_flag=0, m_flag=0, verbose=False)  # Alternative lower cutoff 1000 Hz
        time.sleep(0.001)
        write_intan_register(spi, 8, 0xFFFF, u_flag=0, m_flag=0, verbose=False)  # AC-coupled amplifiers
        time.sleep(0.001)
        write_intan_register(spi, 9, 0xFFFF, u_flag=0, m_flag=0, verbose=False)  # Low-Gain Amplifier Power
        time.sleep(0.001)
        write_intan_register(spi, 10, 0x0000, u_flag=1, m_flag=0, verbose=False)  # Fast settle off
        time.sleep(0.001)
        write_intan_register(spi, 12, 0xFFFF, u_flag=1, m_flag=0, verbose=False)  # Lower cutoff
        time.sleep(0.001)
        
        # 4. Настройка стимуляции
        write_intan_register(spi, 34, 0x00E2, u_flag=0, m_flag=0, verbose=False)  # Step size 1 µA
        time.sleep(0.001)
        write_intan_register(spi, 35, 0x00AA, u_flag=0, m_flag=0, verbose=False)  # PBIAS/NBIAS
        time.sleep(0.001)
        write_intan_register(spi, 36, 0x0080, u_flag=0, m_flag=0, verbose=False)  # Charge recovery voltage
        time.sleep(0.001)
        write_intan_register(spi, 37, 0x4F00, u_flag=0, m_flag=0, verbose=False)  # Charge recovery current limit
        time.sleep(0.001)
        write_intan_register(spi, 42, 0x0000, u_flag=1, m_flag=0, verbose=False)  # Disable all stimulators
        time.sleep(0.001)
        write_intan_register(spi, 44, 0x0000, u_flag=1, m_flag=0, verbose=False)  # Negative polarity
        time.sleep(0.001)
        
        # 5. Инициализация регистров токов стимуляции (64-79 и 96-111) в 0x8000
        for channel in range(16):
            write_intan_register(spi, 64 + channel, 0x8000, u_flag=1, m_flag=0, verbose=False)
            write_intan_register(spi, 96 + channel, 0x8000, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 6. WRITE 32 0xAAAA, WRITE 33 0x00FF - разрешить работу стимуляторов
        write_intan_register(spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 7. Очистка compliance monitor
        clear_compliance_monitor(spi, verbose=False)
        time.sleep(0.001)
        
        if verbose:
            self._log("        ✓ Переинициализация для стимуляции завершена")

    def _initialize_for_recording(self, verbose=False, adc_sampling_rate_ksps=USB_V2_FIRMWARE_ADC_KSPS):
        """
        Инициализация чипа для регистрации данных (без стимуляции).
        
        ВАЖНО: Регистры 32-33 остаются в 0x0000 для дифференциального режима усилителей.
        Это критично для правильной работы EMG регистрации.
        
        Согласно даташиту:
        - Register 0: ADC buffer bias и MUX bias зависят от частоты дискретизации
        - Register 1: DSP фильтр и auxiliary outputs
        - Register 2: Impedance testing DAC (опционально)
        - Register 3: Impedance check DAC (опционально)
        - Registers 4-7, 12: Настройка полосы пропускания усилителей
        - Register 8: High-Gain Amplifier Power
        - Register 9: Low-Gain Amplifier Power
        - Registers 32-33: ДОЛЖНЫ остаться в 0x0000 (не устанавливаем в 0xAAAA/0x00FF)
        
        Args:
            verbose: выводить отладочную информацию
            adc_sampling_rate_ksps: частота дискретизации ADC в kS/s (по умолчанию 350 kS/s)
        """
        spi = self.spi

        if _is_usb_backend(spi):
            ksps = int(adc_sampling_rate_ksps)
            if ksps <= 0:
                ksps = USB_V2_FIRMWARE_ADC_KSPS
            ksps = max(100, min(USB_V2_FIRMWARE_ADC_KSPS, ksps))
            spi.init_record(ksps)
            self._record_ksps = ksps
            if verbose:
                self._log(f"      INIT_RECORD {ksps} kS/s (USB)")
            return

        # 1. READ 255 U=0 M=0 - dummy команда после включения питания
        if verbose:
            self._log("      READ 255 (dummy команда)...")
        read_intan_register(spi, 255, verbose=False)
        time.sleep(0.001)
        
        # 2. WRITE 32/33 0x0000 - отключить стимуляцию (дифференциальный режим усилителей)
        if verbose:
            self._log("      WRITE 32 0x0000, WRITE 33 0x0000 - дифференциальный режим усилителей...")
        write_intan_register(spi, 32, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(spi, 33, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 3. WRITE 38 0xFFFF - включить DC-coupled low-gain amplifiers (ПИТАНИЕ)
        # КРИТИЧНО: В RHS2116 есть аппаратный баг: если выключить DC-усилители,
        # потребление VDD УВЕЛИЧИВАЕТСЯ (~1.93 mA на канал, >30 mA суммарно).
        # Intan ПРЯМО рекомендует всегда включать их (Register 38 = 0xFFFF).
        # Это НЕ означает, что DC-данные используются - это только питание блоков.
        # Чтение DC управляется флагом D в команде CONVERT.
        # Для ЭМГ читаем ТОЛЬКО AC high-gain данные (старшие 16 бит результата CONVERT).
        if verbose:
            self._log("      WRITE 38 0xFFFF - включить DC-coupled amplifiers (питание, не использование данных)...")
        write_intan_register(spi, 38, 0xFFFF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 4. CLEAR - инициализация ADC
        if verbose:
            self._log("      CLEAR - инициализация ADC...")
        clear_adc(spi, verbose=False)
        time.sleep(0.001)
        
        # 5. WRITE 0 - настройка ADC и MUX в зависимости от частоты дискретизации
        # Согласно даташиту, Register 0 содержит ADC buffer bias и MUX bias
        # Для 480 kS/s: ADC buffer bias = 3, MUX bias = 5 (из таблицы даташита)
        # Формат Register 0: биты [15:8] = ADC buffer bias, биты [7:0] = MUX bias
        # Значение 0x00C5 = ADC buffer bias = 0xC (12), MUX bias = 0x5 (5)
        # Но согласно таблице для ≥440 kS/s: ADC buffer bias = 3, MUX bias = 5
        # Правильное значение: (3 << 8) | 5 = 0x0305
        # Однако в StimulationWithPreset используется 0x00C5, проверим:
        # 0x00C5 = 197 = 0b11000101 = ADC buffer bias = 0xC (12), MUX bias = 0x5 (5)
        # Для 480 kS/s используем значение из таблицы: ADC buffer bias = 3, MUX bias = 5
        # Значение: (3 << 8) | 5 = 0x0305
        if verbose:
            self._log(f"      WRITE 0 - настройка ADC (частота дискретизации: {adc_sampling_rate_ksps} kS/s)...")
        
        # Определяем значения bias в зависимости от частоты дискретизации
        if adc_sampling_rate_ksps <= 120:
            adc_buffer_bias = 32
            mux_bias = 40
        elif adc_sampling_rate_ksps <= 140:
            adc_buffer_bias = 16
            mux_bias = 40
        elif adc_sampling_rate_ksps <= 175:
            adc_buffer_bias = 8
            mux_bias = 40
        elif adc_sampling_rate_ksps <= 220:
            adc_buffer_bias = 8
            mux_bias = 32
        elif adc_sampling_rate_ksps <= 280:
            adc_buffer_bias = 8
            mux_bias = 26
        elif adc_sampling_rate_ksps <= 350:
            adc_buffer_bias = 4
            mux_bias = 18
        elif adc_sampling_rate_ksps <= 440:
            adc_buffer_bias = 3
            mux_bias = 16
        else:  # ≥ 440 kS/s
            adc_buffer_bias = 3
            mux_bias = 5
        
        register_0_value = (adc_buffer_bias << 8) | mux_bias
        if verbose:
            self._log(f"        ADC buffer bias: {adc_buffer_bias}, MUX bias: {mux_bias}, Register 0: 0x{register_0_value:04X}")
        write_intan_register(spi, 0, register_0_value, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 6. WRITE 1 - auxiliary outputs и DSP фильтр
        # РЕКОМЕНДУЕМЫЕ НАСТРОЙКИ для ЭМГ + стимуляция (f_sample ≈ 1 kHz):
        # - DSP cutoff = 8-10 (рекомендуется 9)
        # - aux outputs = Hi-Z
        # - absmode = 0 (иначе сигнал всегда положительный)
        # 
        # Структура Register 1:
        # - Биты [15:12] = DSP cutoff frequency (k_freq, 0-15)
        # - Биты [11:8] = Auxiliary outputs
        # - Биты [7:0] = Другие настройки
        #
        # Значение 0x951A:
        # - DSP cutoff = 9 (биты [15:12] = 0x9)
        # - Остальные биты как в примере Intan (0x051A)
        # 
        # Для f_sample ≈ 1 kHz: DSP f_c = k_freq * f_sample
        # При k_freq = 9: f_c ≈ 9 Hz (эффективно убирает хвосты после стимуляции)
        if verbose:
            self._log("      WRITE 1 0x951A - auxiliary outputs и DSP фильтр (DSP cutoff=9 для ЭМГ+стимуляция)...")
        write_intan_register(spi, 1, 0x951A, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 7. WRITE 2 0x0000 - Zcheck выключен (экономия 120 µA, включается только при measure_impedance)
        write_intan_register(spi, 2, 0x0000, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 8. WRITE 3 0x0080 - нейтральное значение impedance DAC (при выкл. Reg2 не влияет)
        write_intan_register(spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 9. WRITE 4 - верхняя частота среза AC-coupled amplifiers (fH)
        # Для ЭМГ предплечья рекомендуется fH = 500 Hz (вместо 7.5 kHz)
        # Значение из даташита RHS2116 для 500 Hz: 0x015E
        # Альтернатива для 750 Hz: 0x00E9 (закомментировано)
        if verbose:
            self._log("      WRITE 4 0x015E - верхняя частота среза (500 Hz для ЭМГ)...")
        write_intan_register(spi, 4, 0x015E, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 10. WRITE 5 - параметр для верхней частоты среза (fH)
        # Для fH = 500 Hz значение: 0x01AB
        # Для fH = 750 Hz значение: 0x0124 (закомментировано)
        if verbose:
            self._log("      WRITE 5 0x01AB - параметр для верхней частоты среза (500 Hz)...")
        write_intan_register(spi, 5, 0x01AB, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 11. WRITE 6 - нижняя частота среза AC-coupled amplifiers (fL, версия A)
        # Для ЭМГ предплечья рекомендуется fL = 20 Hz (вместо 5 Hz)
        # Значение из даташита RHS2116 для 20 Hz: 0x0036
        if verbose:
            self._log("      WRITE 6 0x0036 - нижняя частота среза (20 Hz, версия A для ЭМГ)...")
        write_intan_register(spi, 6, 0x0036, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 12. WRITE 7 0x000A - RL_B (быстрое восстановление, версия B)
        # Оставляем как в примере Intan
        if verbose:
            self._log("      WRITE 7 0x000A - RL_B (быстрое восстановление, версия B)...")
        write_intan_register(spi, 7, 0x000A, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 13. WRITE 8 0xFFFF - включить все AC-coupled high-gain amplifiers
        if verbose:
            self._log("      WRITE 8 0xFFFF - включить AC-coupled amplifiers...")
        write_intan_register(spi, 8, 0xFFFF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 14. WRITE 9 0xFFFF - Low-Gain Amplifier Power
        # ВАЖНО: Назначение Register 9 в RHS2116 не подтверждено даташитом как управление low-gain power.
        # В RHS2116:
        # - AC amplifier power -> Register 8
        # - DC amplifier power -> Register 38
        # НЕ МЕНЯТЬ Register 9, пока его назначение не подтверждено страницей даташита RHS2116.
        if verbose:
            self._log("      WRITE 9 0xFFFF - Low-Gain Amplifier Power (не менять без подтверждения даташитом)...")
        write_intan_register(spi, 9, 0xFFFF, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 15. WRITE 10 0x0000 U=1 - отключить fast settle (triggered register)
        if verbose:
            self._log("      WRITE 10 0x0000 U=1 - отключить fast settle...")
        write_intan_register(spi, 10, 0x0000, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 16. WRITE 12 0xFFFF U=1 - установить все amplifiers на нижнюю частоту среза версии A (triggered register)
        # Использует Register 6 (fL = 20 Hz для ЭМГ)
        if verbose:
            self._log("      WRITE 12 0xFFFF U=1 - установить нижнюю частоту среза (версия A, 20 Hz для ЭМГ)...")
        write_intan_register(spi, 12, 0xFFFF, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # КРИТИЧНО: Регистры 32-33 НЕ устанавливаем в 0xAAAA/0x00FF
        # Они должны остаться в 0x0000 для дифференциального режима усилителей
        # Это необходимо для правильной работы EMG регистрации
        
        if verbose:
            self._log("      ✓ Инициализация для регистрации завершена (регистры 32-33 остались в 0x0000)")

    def convert_channel(self, channel, amp_type='ac', h_flag=0):
        """
        Выполняет CONVERT команду для канала и возвращает ADC значение.
        
        Согласно даташиту RHS2116 (стр. 32):
        - Биты [31:30] = 00 (команда CONVERT)
        - Бит [29] = U (Update flag) = 0
        - Бит [28] = M (Monitor flag) = 0
        - Бит [27] = D (DC amplifier flag) = 1 для чтения DC, 0 для только AC
        - Бит [26] = H (High-pass filter flag) = 0 (обычно) или 1 (для сброса DSP HPF)
        - Биты [25:22] = 0000
        - Биты [21:16] = C[5:0] (номер канала, 6 бит, 0-15)
        - Биты [15:0] = 0x0000
        
        КРИТИЧНО: H flag = 1 мгновенно сбрасывает DSP high-pass filter.
        Это ОБЯЗАТЕЛЬНО после стимуляции для быстрого восстановления baseline.
        
        Результат:
        - Биты [31:16] = A[15:0] (AC high-gain amplifier, 16 бит)
        - Биты [15:10] = 000000
        - Биты [9:0] = W[9:0] (DC low-gain amplifier, 10 бит, только если D=1)
        
        Args:
            channel: номер канала (0-15)
            amp_type: тип усилителя ('ac' для high-gain, 'dc' для low-gain)
            h_flag: флаг сброса DSP HPF (0 = обычный режим, 1 = сброс фильтра)
        
        Returns:
            16-битное значение ADC (знаковое) для AC усилителя
        """
        if channel < 0 or channel > 15:
            raise ValueError(f"Номер канала должен быть 0-15, получено: {channel}")

        return convert_intan(
            self.spi, channel, amp_type=amp_type, h_flag=h_flag, verbose=self.verbose
        )

    def reset_dsp_hpf(self, channel=0):
        """
        Сбрасывает DSP high-pass filter для указанного канала.
        
        КРИТИЧНО: После стимуляции DSP HPF может быть насыщен остаточными DC-смещениями.
        Сброс через H flag мгновенно восстанавливает baseline.
        
        Согласно даташиту: "Each channel's DSP high-pass filter can be instantly reset
        by setting the H flag of the CONVERT command to one."
        
        Args:
            channel: номер канала (0-15), по умолчанию 0
        
        Returns:
            16-битное значение ADC (беззнаковое, 0-65535)
        """
        return self.convert_channel(channel, amp_type='ac', h_flag=1)

    def convert_channel_auto(self):
        """
        Выполняет CONVERT(63) для автоматического переключения на следующий канал.
        """
        return convert_intan(self.spi, 63, amp_type="ac", h_flag=0, verbose=self.verbose)

    def _convert_1tx(self, cmd_bytes):
        """
        Одна SPI-транзакция CONVERT. Результат приходит с задержкой 2 (pipeline).
        Ответ текущей транзакции = результат команды N-2.
        Используется в record_channels для максимальной скорости.
        """
        if _is_usb_backend(self.spi):
            channel = cmd_bytes[1] & 0x3F
            return convert_intan(self.spi, channel, amp_type="ac", h_flag=0)
        resp = self.spi.transfer(cmd_bytes)
        return (resp[0] << 8) | resp[1]

    def read_temperature(self):
        """
        Читает температуру из регистра 3 (Temperature Sensor).
        
        Returns:
            Значение температуры (16-битное значение ADC)
        """
        self.ensure_initialized()
        temp_value = read_intan_register(self.spi, 3, verbose=self.verbose)
        return temp_value

    def measure_impedance(self, channel, test_current_nA=5, frequency=1000, num_samples=5000):
        """
        Измеряет импеданс электрода на указанном канале.
        
        Согласно даташиту RHS2116 (стр. 17-19, 36-37):
        - Zcheck использует ТОЛЬКО Register 2 и Register 3 (рег. 15-20 не существуют)
        - Register 2: Zcheck en, Zcheck select (канал), Zcheck scale (CS: 0.1/1/10 pF), Zcheck DAC power
        - Register 3: 8-бит DAC — ОБЯЗАТЕЛЬНО обновлять во времени для синусоиды!
        - I_peak = 2*pi*f*CS*VA; при 1 kHz: 0.1pF→0.38nA, 1pF→3.8nA, 10pF→38nA (макс. VA=0.6125V)
        - Z = V_peak / I_peak; AC усилители насыщаются при |Vin| > ±5 mV
        
        Args:
            channel: номер канала (0-15)
            test_current_nA: целевой пиковый ток в nA (по умолчанию 5 nA)
            frequency: частота тестового сигнала в Hz (по умолчанию 1000 Hz)
            num_samples: желаемое число samples (округлится до целых периодов)
        
        Returns:
            dict с impedance_ohm, adc_rms, voltage_rms, test_current_nA, frequency, channel
        """
        # ВАЖНО: Сохраняем состояние ВСЕХ регистров стимуляции ДО инициализации
        # потому что ensure_initialized() может вызвать _initialize_for_recording(),
        # которая установит регистры 32-33 в 0x0000
        # Регистры 64-79 и 96-111 являются triggered registers и требуют флага U для активации
        if self.spi is not None:
            try:
                reg32_before = read_intan_register(self.spi, 32, verbose=False)
                reg33_before = read_intan_register(self.spi, 33, verbose=False)
                reg42_before = read_intan_register(self.spi, 42, verbose=False)
                reg44_before = read_intan_register(self.spi, 44, verbose=False)
                reg2_before = read_intan_register(self.spi, 2, verbose=False)
                reg3_before = read_intan_register(self.spi, 3, verbose=False)
                reg34_before = read_intan_register(self.spi, 34, verbose=False)  # Step size
                reg35_before = read_intan_register(self.spi, 35, verbose=False)  # PBIAS/NBIAS
                
                # Сохраняем все регистры токов стимуляции (64-79 для отрицательных, 96-111 для положительных)
                regs_64_79_before = []  # Отрицательные токи (triggered registers)
                regs_96_111_before = []  # Положительные токи (triggered registers)
                for ch in range(16):
                    regs_64_79_before.append(read_intan_register(self.spi, 64 + ch, verbose=False))
                    regs_96_111_before.append(read_intan_register(self.spi, 96 + ch, verbose=False))
            except:
                # Если не удалось прочитать (чип не инициализирован), используем значения по умолчанию
                reg32_before = 0x0000
                reg33_before = 0x0000
                reg42_before = 0x0000
                reg44_before = 0x0000
                reg2_before = 0x0000
                reg3_before = 0x0000
                reg34_before = 0x0000
                reg35_before = 0x0000
                regs_64_79_before = [0x8000] * 16  # Значение по умолчанию для токов
                regs_96_111_before = [0x8000] * 16
        else:
            reg32_before = 0x0000
            reg33_before = 0x0000
            reg42_before = 0x0000
            reg44_before = 0x0000
            reg2_before = 0x0000
            reg3_before = 0x0000
            reg34_before = 0x0000
            reg35_before = 0x0000
            regs_64_79_before = [0x8000] * 16
            regs_96_111_before = [0x8000] * 16
        
        # Теперь инициализируем (это может изменить регистры 32-33)
        self.ensure_initialized()
        
        if channel < 0 or channel > 15:
            raise ValueError(f"Номер канала должен быть 0-15, получено: {channel}")
        
        if test_current_nA <= 0 or test_current_nA > 1000:
            raise ValueError(f"Тестовый ток должен быть в диапазоне 0-1000 nA, получено: {test_current_nA} nA")
        
        if self.verbose:
            self._log(f"Измерение импеданса: канал {channel}, ток {test_current_nA} nA, частота {frequency} Hz")
            self._log(f"      Сохранено состояние: Reg32=0x{reg32_before:04X}, Reg33=0x{reg33_before:04X}, Reg42=0x{reg42_before:04X}, Reg44=0x{reg44_before:04X}")
            self._log(f"      Reg2=0x{reg2_before:04X}, Reg3=0x{reg3_before:04X}, Reg34=0x{reg34_before:04X}, Reg35=0x{reg35_before:04X}")
            self._log(f"      Регистры токов стимуляции сохранены (64-79, 96-111)")
        
        # --- RHS2116 Zcheck: ТОЛЬКО Register 2 и 3 (рег. 15-20 не существуют) ---
        # Register 2: D[13:8]=channel, D[6]=DAC power, D[5]=load(0), D[4:3]=scale, D[0]=en
        # Zcheck scale: 00=0.1pF, 01=1pF, 11=10pF. Ток при 1kHz макс: 0.38/3.8/38 nA
        if test_current_nA <= 0.38:
            cs_scale = 0   # 0.1 pF
            cs_pF = 0.1
            i_max_nA = 0.38
        elif test_current_nA <= 3.8:
            cs_scale = 1   # 1 pF
            cs_pF = 1.0
            i_max_nA = 3.8
        else:
            cs_scale = 3   # 10 pF (биты 11)
            cs_pF = 10.0
            i_max_nA = 38.0
        amp_factor = min(1.0, test_current_nA / i_max_nA)  # 0..1
        dac_amplitude = max(1, int(127 * amp_factor))  # 1..127 от средней 128
        # Фактический ток: I_peak = 2*pi*f*CS*VA, VA=(dac_amp/127)*0.6125V
        va_volts = (dac_amplitude / 127.0) * 0.6125
        actual_current_nA = 2.0 * math.pi * frequency * cs_pF * 1e-12 * va_volts * 1e9
        
        reg2 = (channel << 8) | (1 << 6) | (1 << 0) | (cs_scale << 3)  # DAC power, Zcheck en, scale
        if self.verbose:
            self._log(f"      Register 2: канал {channel}, CS={cs_pF} pF, DAC amp={dac_amplitude}, I_peak≈{actual_current_nA:.2f} nA")
        write_intan_register(self.spi, 2, reg2, u_flag=0, m_flag=0, verbose=self.verbose)
        time.sleep(0.02)  # стабилизация DAC и переключателей
        
        # Сбор samples: КРИТИЧНО — обновлять Register 3 синусом на каждой итерации
        sample_rate_hz = 30000  # практическая частота CONVERT при одиночном канале
        samples_per_period = max(10, int(sample_rate_hz / frequency))
        num_periods = max(5, int(num_samples / samples_per_period))
        actual_samples = num_periods * samples_per_period
        
        if self.verbose:
            self._log(f"      Сбор {actual_samples} samples ({num_periods} периодов), обновление DAC по синусу...")
        
        adc_samples = []
        for i in range(actual_samples):
            phase = 2.0 * math.pi * (i % samples_per_period) / samples_per_period
            dac_val = 128 + int(dac_amplitude * math.sin(phase))
            dac_val = max(1, min(255, dac_val))
            write_intan_register(self.spi, 3, dac_val & 0xFF, u_flag=0, m_flag=0, verbose=False)
            adc_value = self.convert_channel(channel, amp_type='ac')
            adc_samples.append(adc_value)
            if i % 500 == 0 and self.verbose:
                self._log(f"        Sample {i}/{actual_samples}...")
        
        # Отключаем Zcheck
        write_intan_register(self.spi, 2, 0x0000, u_flag=0, m_flag=0, verbose=False)
        write_intan_register(self.spi, 3, 0x0080, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # 8a. ВАЖНО: Восстанавливаем состояние регистров стимуляции после измерения
        # Это необходимо, чтобы стимуляция продолжала работать после измерения импеданса
        if self.verbose:
            self._log("      Восстановление состояния регистров стимуляции...")
        
        # Восстанавливаем регистры 32-33 в исходное состояние
        # Это критично для работы стимуляции после измерения импеданса
        write_intan_register(self.spi, 32, reg32_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 33, reg33_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # Проверяем, что регистры действительно восстановлены
        reg32_after = read_intan_register(self.spi, 32, verbose=False)
        reg33_after = read_intan_register(self.spi, 33, verbose=False)
        
        if self.verbose:
            if reg32_before == 0xAAAA and reg33_before == 0x00FF:
                if reg32_after == 0xAAAA and reg33_after == 0x00FF:
                    self._log("        Регистры 32-33 восстановлены в 0xAAAA/0x00FF для стимуляции ✓")
                else:
                    self._log(f"        ⚠ Регистры 32-33 не восстановлены! Ожидалось 0xAAAA/0x00FF, получено 0x{reg32_after:04X}/0x{reg33_after:04X}")
                    # Принудительно восстанавливаем
                    write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
                    time.sleep(0.001)
                    write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
                    time.sleep(0.001)
                    self._log("        Регистры 32-33 принудительно восстановлены в 0xAAAA/0x00FF")
            else:
                self._log(f"        Регистры 32-33 восстановлены в 0x{reg32_before:04X}/0x{reg33_before:04X}")
        
        # Восстанавливаем регистры 42 и 44 (стимуляция)
        # Эти регистры являются triggered registers и требуют u_flag=1
        write_intan_register(self.spi, 42, reg42_before, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 44, reg44_before, u_flag=1, m_flag=0, verbose=False)
        time.sleep(0.001)
        
        # Проверяем, что регистры действительно восстановлены
        reg42_after = read_intan_register(self.spi, 42, verbose=False)
        reg44_after = read_intan_register(self.spi, 44, verbose=False)
        
        if self.verbose:
            if reg42_after == reg42_before and reg44_after == reg44_before:
                self._log(f"        Регистры 42/44 восстановлены: 0x{reg42_before:04X}/0x{reg44_before:04X} ✓")
            else:
                self._log(f"        ⚠ Регистры 42/44 не восстановлены! Ожидалось 0x{reg42_before:04X}/0x{reg44_before:04X}, получено 0x{reg42_after:04X}/0x{reg44_after:04X}")
                # Принудительно восстанавливаем
                write_intan_register(self.spi, 42, reg42_before, u_flag=1, m_flag=0, verbose=False)
                time.sleep(0.001)
                write_intan_register(self.spi, 44, reg44_before, u_flag=1, m_flag=0, verbose=False)
                time.sleep(0.001)
                self._log(f"        Регистры 42/44 принудительно восстановлены в 0x{reg42_before:04X}/0x{reg44_before:04X}")
        
        # Восстанавливаем регистры 2 и 3 (impedance testing DAC)
        # Эти регистры могут быть изменены во время измерения импеданса
        write_intan_register(self.spi, 2, reg2_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 3, reg3_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        if self.verbose:
            self._log(f"        Регистры 2/3 восстановлены: 0x{reg2_before:04X}/0x{reg3_before:04X}")
        
        # Восстанавливаем регистры 34-35 (step size и PBIAS/NBIAS)
        # Эти регистры важны для правильной работы стимуляторов
        write_intan_register(self.spi, 34, reg34_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        write_intan_register(self.spi, 35, reg35_before, u_flag=0, m_flag=0, verbose=False)
        time.sleep(0.001)
        if self.verbose:
            self._log(f"        Регистры 34/35 восстановлены: 0x{reg34_before:04X}/0x{reg35_before:04X}")
        
        # КРИТИЧНО: Если регистры были в состоянии для стимуляции, выполняем полную переинициализацию
        # Это гарантирует, что все регистры стимуляции будут правильно настроены
        # ВАЖНО: Переинициализация должна быть ДО восстановления регистров токов,
        # так как она устанавливает их в 0x8000, а потом мы восстановим нужные значения
        if reg32_before == 0xAAAA and reg33_before == 0x00FF:
            if self.verbose:
                self._log("        Выполняется полная переинициализация для стимуляции...")
            self._reinitialize_for_stimulation(verbose=self.verbose)
            
            # Дополнительно: убеждаемся, что регистры 32-33 установлены правильно
            reg32_final = read_intan_register(self.spi, 32, verbose=False)
            reg33_final = read_intan_register(self.spi, 33, verbose=False)
            if reg32_final != 0xAAAA or reg33_final != 0x00FF:
                if self.verbose:
                    self._log(f"        ⚠ Регистры 32-33 не установлены! Принудительно устанавливаем...")
                write_intan_register(self.spi, 32, 0xAAAA, u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                write_intan_register(self.spi, 33, 0x00FF, u_flag=0, m_flag=0, verbose=False)
                time.sleep(0.001)
                clear_compliance_monitor(self.spi, verbose=False)
                time.sleep(0.001)
                if self.verbose:
                    self._log("        ✓ Регистры 32-33 принудительно установлены в 0xAAAA/0x00FF")
            
            # Финальная проверка всех критических регистров
            if self.verbose:
                reg32_check = read_intan_register(self.spi, 32, verbose=False)
                reg33_check = read_intan_register(self.spi, 33, verbose=False)
                reg34_check = read_intan_register(self.spi, 34, verbose=False)
                reg35_check = read_intan_register(self.spi, 35, verbose=False)
                self._log(f"        Финальная проверка: Reg32=0x{reg32_check:04X}, Reg33=0x{reg33_check:04X}, Reg34=0x{reg34_check:04X}, Reg35=0x{reg35_check:04X}")
                if reg32_check == 0xAAAA and reg33_check == 0x00FF:
                    self._log("        ✓ Чип готов к стимуляции")
                else:
                    self._log("        ⚠ ВНИМАНИЕ: Регистры 32-33 не в состоянии для стимуляции!")
        else:
            # Если не было стимуляции, просто очищаем compliance monitor
            if self.verbose:
                self._log("        Очистка compliance monitor...")
            clear_compliance_monitor(self.spi, verbose=False)
            time.sleep(0.001)
        
        # 9. Обработка данных
        # Конвертируем ADC значения в знаковые (0-65535 -> -32768 to 32767)
        # Если значение >= 32768, это отрицательное число в дополнении до двух
        adc_signed_list = [(x - 65536) if x >= 32768 else x for x in adc_samples]
        
        # КРИТИЧНО: Вычитаем DC-смещение (среднее) перед расчётом RMS.
        # Без этого любое смещение (электрод, помеха 50/60 Hz) искажает результат
        # и может давать фиксированное ~2.4 МОм при измерении паразитной ёмкости.
        if NUMPY_AVAILABLE:
            adc_signed = np.array(adc_signed_list)
            dc_offset = np.mean(adc_signed)
            adc_ac = adc_signed - dc_offset
            adc_rms = np.sqrt(np.mean(adc_ac ** 2))
        else:
            dc_offset = sum(adc_signed_list) / len(adc_signed_list) if adc_signed_list else 0
            adc_ac = [x - dc_offset for x in adc_signed_list]
            adc_squared = [x * x for x in adc_ac]
            adc_mean_squared = sum(adc_squared) / len(adc_squared) if len(adc_squared) > 0 else 0
            adc_rms = math.sqrt(adc_mean_squared)
        
        # Конвертируем ADC RMS в напряжение (µV)
        # Согласно даташиту: Velec(AC) = 0.195 µV × (ADC result – 32768)
        # Для RMS: V_rms = 0.195 µV × ADC_rms
        voltage_rms_uv = 0.195 * adc_rms
        
        # Вычисляем пиковое напряжение (RMS × √2 для синусоидального сигнала)
        voltage_peak_uv = voltage_rms_uv * math.sqrt(2)
        
        # Конвертируем в вольты
        voltage_peak_v = voltage_peak_uv * 1e-6  # µV -> V
        
        # Используем фактический ток (из DAC amplitude и CS)
        current_peak_a = actual_current_nA * 1e-9  # nA -> A
        
        # Вычисляем импеданс: Z = V_peak / I_peak
        if current_peak_a > 0 and adc_rms > 5:
            impedance_ohm = voltage_peak_v / current_peak_a
            if impedance_ohm > 100e6:
                impedance_ohm = 100e6
        else:
            impedance_ohm = 0.0
        
        # КРИТИЧНО: Учитываем паразитную ёмкость согласно даташиту
        # Паразитная ёмкость 10 pF имеет импеданс ≈16 MΩ на 1 kHz
        # Это влияет на измерения для электродов с высоким импедансом (>1 MΩ)
        # Для более точных измерений можно вычесть эту ёмкость, но это опционально
        # parasitic_capacitance_impedance = 1 / (2 * math.pi * frequency * 10e-12)  # 10 pF
        # if impedance_ohm > parasitic_capacitance_impedance * 0.1:  # Если импеданс > 10% от паразитного
        #     # Корректируем импеданс (опционально, может быть неточным)
        #     pass
        
        dc_offset_val = float(dc_offset)
        if self.verbose:
            self._log(f"      Результаты измерения:")
            self._log(f"        DC offset (ADC): {dc_offset_val:.1f}")
            self._log(f"        ADC RMS (AC): {adc_rms:.2f}")
            self._log(f"        U_RMS: {voltage_rms_uv:.2f} µV")
            self._log(f"        U_peak: {voltage_peak_uv:.2f} µV")
            self._log(f"        I_peak: {actual_current_nA:.2f} nA")
            self._log(f"        Импеданс: {impedance_ohm:.2f} Ω ({impedance_ohm/1e6:.2f} MΩ)")
            if adc_rms < 20:
                self._log(f"        ⚠ Слабый сигнал (ADC RMS < 20) — проверьте подключение электрода")
        
        return {
            "impedance_ohm": float(impedance_ohm),
            "adc_rms": float(adc_rms),
            "dc_offset": float(dc_offset_val),
            "voltage_rms": float(voltage_rms_uv),  # в µV
            "voltage_peak": float(voltage_peak_uv),  # в µV
            "test_current_nA": float(actual_current_nA),
            "frequency": float(frequency),
            "channel": int(channel)
        }

    def record_channels(self, channels, sample_rate, duration=None):
        """
        Регистрирует данные с указанных каналов.
        1 транзакция на CONVERT — сырой поток. Смещение pipeline обрабатывается в GUI.
        
        Yields:
            (timestamp, raw_values, channels, pipeline_skip)
            raw_values — сырые uint16 как приходят из SPI (первые pipeline_skip — мусор)
            pipeline_skip — сколько значений пропустить в начале (Intan pipeline = 2 циклов)
        """
        self.ensure_initialized()
        
        if sample_rate <= 0:
            raise ValueError("Частота дискретизации должна быть > 0")

        if _is_usb_backend(self.spi):
            yield from self._record_channels_usb(channels, sample_rate, duration)
            return

        start_time = time.time()
        conv_63 = [0x00, 0x3F, 0x00, 0x00]
        conv_ch = lambda ch: [0x00, ch & 0x3F, 0x00, 0x00]
        
        sequential = (len(channels) > 1 and channels == list(range(channels[0], channels[0] + len(channels))))
        nch = len(channels)
        first_ch = channels[0]
        # Pipeline Intan: ответ N = результат команды N-2. Первые 4 ответа — мусор.
        PIPELINE_SKIP = 4
        
        self._log(f"Начало регистрации: каналы {channels} (1 tx/convert, макс. скорость без sleep)")
        
        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break
                
                sample_start = time.time()
                raw_values = []
                
                if sequential:
                    # 2 dummy + CONVERT(0) + 17×CONVERT(63) = 20 tx; ответы 1–4 мусор, 5–20 = ch0..ch15
                    for _ in range(2):
                        raw_values.append(self._convert_1tx(conv_63))
                    raw_values.append(self._convert_1tx(conv_ch(first_ch)))
                    for _ in range(17):
                        raw_values.append(self._convert_1tx(conv_63))
                else:
                    for _ in range(2):
                        raw_values.append(self._convert_1tx(conv_63))
                    for ch in channels:
                        raw_values.append(self._convert_1tx(conv_ch(ch)))
                    for _ in range(nch + 2):
                        raw_values.append(self._convert_1tx(conv_63))
                
                yield (sample_start, raw_values, channels, PIPELINE_SKIP)
        except KeyboardInterrupt:
            self._log("Регистрация прервана пользователем")
        except Exception as e:
            self._log(f"Ошибка в record_channels: {e}")
            raise

    def _record_channels_usb(self, channels, sample_rate, duration=None):
        """
        Регистрация через USB (STM32 coprocessor).

        V2 (RHS1): SPI_STREAM_RR8_REAL / SPI_STREAM_REAL → кадры 4096 B.
        V1 (legacy): STREAM / STREAM8 → сырой uint16 bulk.
        """
        if hasattr(self.spi, "firmware_version"):
            try:
                firmware = self.spi.firmware_version()
            except Exception as exc:
                self._log(f"⚠ Не удалось определить прошивку USB V2: {exc}")
            else:
                if firmware == "v2":
                    yield from self._record_channels_usb_v2(
                        channels, sample_rate, duration
                    )
                    return

        yield from self._record_channels_usb_v1(channels, sample_rate, duration)

    def _record_channels_usb_v2(self, channels, sample_rate, duration=None):
        """USB HS Streaming V2 — RHS1 ring, SPI_STREAM_RR8_REAL / SPI_STREAM_REAL."""
        start_time = time.time()
        nch = len(channels)
        PIPELINE_SKIP = 0
        spi = self.spi
        use_rr8 = channels == list(range(RR8_CHANNELS))
        use_rr16 = channels == list(range(16))
        sequential = (
            nch > 1
            and channels == list(range(channels[0], channels[0] + nch))
        )
        channel_router = Rhs1ChannelRouter(channels) if nch > 1 else None
        frame_timeout_ms = 250
        overflow_logged = False
        metadata_logged = False
        tag_errors_logged = False
        tagged_mismatch_logged = False
        yield_buf = bytearray()
        batch_meta = None
        batch_wall_ts = 0.0
        batch_capture_ns = 0
        frame_bytes = nch * 2

        if not (nch == 1 or sequential):
            raise RuntimeError(
                "USB V2 STM32 поддерживает real recording одного канала или "
                f"непрерывного диапазона каналов; запрошено {channels}"
            )

        self._log(
            f"USB V2 RHS1: каналы {channels}, цель {sample_rate} Hz/канал, "
            f"режим={'RR16' if use_rr16 else ('RR8' if use_rr8 else ('range' if nch > 1 else '1ch'))}, "
            f"yield≥{USB_V2_YIELD_TARGET_BYTES}B, burst+{USB_V2_BURST_EXTRA_FRAMES}, "
            f"udp_wifi={INTAN_UDP_WIFI}"
        )

        def reload_stream() -> None:
            nonlocal batch_meta
            batch_meta = None
            yield_buf.clear()
            if use_rr16:
                spi.start_spi_stream_rr16_real(V2_STREAM_RELOAD_SAMPLES, 0)
            elif use_rr8:
                spi.start_spi_stream_rr8_real(V2_STREAM_RELOAD_SAMPLES, 0)
            elif nch == 1:
                spi.start_spi_stream_real(V2_STREAM_RELOAD_SAMPLES, channels[0], 0)
            else:
                spi.start_spi_stream_range_real(
                    V2_STREAM_RELOAD_SAMPLES, channels[0], nch, 0
                )

        def emit_batch(*, force: bool = False):
            nonlocal batch_meta, batch_wall_ts, batch_capture_ns
            if batch_meta is None or not yield_buf:
                return
            n_frames = len(yield_buf) // frame_bytes
            if n_frames <= 0:
                return
            if not force and len(yield_buf) < USB_V2_YIELD_TARGET_BYTES:
                return
            use_bytes = n_frames * frame_bytes
            chunk = bytes(yield_buf[:use_bytes])
            del yield_buf[:use_bytes]
            yield _rhs1_stream_batch(
                batch_meta,
                chunk,
                channels,
                pipeline_skip=PIPELINE_SKIP,
                wall_ts=batch_wall_ts,
                capture_ts_ns=batch_capture_ns,
            )
            batch_meta = None

        reload_stream()
        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break

                if not getattr(spi, "_v2_stream_active", False):
                    reload_stream()

                raw_frames = spi.read_rhs1_raw_burst(
                    timeout_ms=frame_timeout_ms,
                    max_extra=USB_V2_BURST_EXTRA_FRAMES,
                )
                if not raw_frames:
                    continue

                for raw in raw_frames:
                    meta = parse_rhs1_header(raw)

                    if (
                        (meta.spi_overflow_count or meta.usb_overflow_count)
                        and not overflow_logged
                    ):
                        self._log(
                            f"⚠ RHS1 overflow: spi={meta.spi_overflow_count}, "
                            f"usb={meta.usb_overflow_count}"
                        )
                        overflow_logged = True

                    if not metadata_logged:
                        tagged = bool(meta.flags & RHS1_FLAG_CHANNEL_TAG)
                        self._log(
                            "RHS1 metadata: "
                            f"first_channel={meta.first_channel}, "
                            f"channel_count={meta.channel_count}, "
                            f"flags=0x{meta.flags:04X}, "
                            f"channel_tagged={tagged}, "
                            f"channel_bits={meta.channel_bits}, "
                            f"convert_flags=0x{meta.convert_flags:02X}"
                        )
                        metadata_logged = True

                    if nch == 1:
                        if meta.channel_tagged and meta.channel_count > 1:
                            if not tagged_mismatch_logged:
                                self._log(
                                    "⚠ RHS1: 1ch запись получила tagged multi-ch кадр "
                                    f"(channel_count={meta.channel_count}); перезапуск потока"
                                )
                                tagged_mismatch_logged = True
                            reload_stream()
                            break
                        chunk = rhs1_raw_payload_bytes(raw, channels, meta)
                        if len(chunk) < 2:
                            continue
                    elif channel_router is not None:
                        chunk = channel_router.feed_raw(raw, meta, validate_tags=False)
                        if (
                            channel_router.tag_errors > 0
                            and not tag_errors_logged
                        ):
                            self._log(
                                f"⚠ RHS1 channel tag mismatch: "
                                f"errors={channel_router.tag_errors}"
                            )
                            tag_errors_logged = True
                        if not chunk:
                            continue
                    else:
                        continue

                    if batch_meta is None:
                        batch_meta = meta
                        batch_wall_ts = time.time()
                        batch_capture_ns = time.time_ns() if INTAN_UDP_USE_V6 else 0
                    yield_buf.extend(chunk)

                    for out in emit_batch():
                        yield out

            for out in emit_batch(force=True):
                yield out
        except KeyboardInterrupt:
            self._log("Регистрация прервана пользователем")
        except Exception as e:
            self._log(f"Ошибка в _record_channels_usb_v2: {e}")
            raise
        finally:
            try:
                spi.stop_stream()
            except Exception:
                pass

    def _record_channels_usb_v1(self, channels, sample_rate, duration=None):
        """Legacy USB: STREAM / STREAM8."""
        start_time = time.time()
        nch = len(channels)
        PIPELINE_SKIP = 0
        sequential = (
            nch > 1
            and channels == list(range(channels[0], channels[0] + nch))
        )
        # Пакет: кратно числу каналов в кадре (плотная раскладка v3)
        if nch == 1:
            usb_batch = 4096
        elif sequential and channels[0] == 0 and nch == MUX_FRAME_CHANNELS:
            usb_batch = 4096  # mux63: 256 кадров × 16
        else:
            frames_per_batch = 512
            usb_batch = frames_per_batch * nch
            usb_batch = min(4096, (usb_batch // nch) * nch)

        self._log(
            f"USB V1 STREAM: каналы {channels}, batch={usb_batch}, "
            f"sequential={sequential}, цель {sample_rate} Hz/канал"
        )

        try:
            if nch == 1:
                stream_ch = channels[0]
                while True:
                    if duration and (time.time() - start_time) >= duration:
                        break
                    payload = self.spi.stream(usb_batch, stream_ch, 0)
                    if len(payload) < 2:
                        continue
                    yield StreamBatch(time.time(), payload, channels, PIPELINE_SKIP)
                return

            ch_first = channels[0]
            ch_last = channels[-1]
            use_stream8 = channels == list(range(8))

            if use_stream8 and self._usb_stream8_ok is None:
                try:
                    probe = self.spi.stream8(64, 0)
                    self._usb_stream8_ok = len(probe) == 128
                except Exception:
                    self._usb_stream8_ok = False
                if self._usb_stream8_ok:
                    self._log("USB STREAM8 (каналы 0–7): прошивка поддерживает")
                else:
                    self._log(
                        "USB STREAM8 недоступен — обновите прошивку STM32_SPI_CO; "
                        "fallback STREAM range / CONVERT(63)"
                    )

            if sequential and not (use_stream8 and self._usb_stream8_ok):
                if self._usb_stream_range_ok is None:
                    try:
                        probe = self.spi.stream(nch * 16, ch_first, 0, ch_last=ch_last)
                        self._usb_stream_range_ok = len(probe) == nch * 16 * 2
                    except Exception:
                        self._usb_stream_range_ok = False
                    if self._usb_stream_range_ok:
                        self._log(
                            f"USB STREAM range {ch_first}-{ch_last}: прошивка поддерживает"
                        )

            if use_stream8 and self._usb_stream8_ok:
                while True:
                    if duration and (time.time() - start_time) >= duration:
                        break
                    payload = self.spi.stream8(usb_batch, 0)
                    if len(payload) < nch * 2:
                        continue
                    yield StreamBatch(time.time(), payload, channels, PIPELINE_SKIP)
                return

            if sequential:
                while True:
                    if duration and (time.time() - start_time) >= duration:
                        break
                    if self._usb_stream_range_ok:
                        payload = self.spi.stream(
                            usb_batch, ch_first, 0, ch_last=ch_last
                        )
                    else:
                        payload = self.spi.stream(usb_batch, 63, 0)
                        payload = _extract_mux63_payload(payload, channels)
                    if len(payload) < nch * 2:
                        continue
                    yield StreamBatch(time.time(), payload, channels, PIPELINE_SKIP)
                return

            # Произвольный набор каналов: mux63 + выборка (медленнее)
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break
                payload = self.spi.stream(usb_batch, 63, 0)
                if len(payload) < MUX_FRAME_CHANNELS * 2:
                    continue
                compact = _extract_mux63_payload(payload, channels)
                if not compact:
                    continue
                yield StreamBatch(time.time(), compact, channels, PIPELINE_SKIP)
        except KeyboardInterrupt:
            self._log("Регистрация прервана пользователем")
        except Exception as e:
            self._log(f"Ошибка в _record_channels_usb_v1: {e}")
            raise

    def close(self):
        """
        Безопасно освобождает ресурсы recorder.
        Метод нужен для корректной остановки из intan_server.py.
        """
        self.recording = False
        self.initialized = False

        if self.backend == "usb":
            self.spi = self.transport
            return

        try:
            if self.spi is not None and not _is_usb_backend(self.spi):
                self.spi.close()
        except Exception:
            pass
        finally:
            self.spi = None


class UDPRecorderServer:
    """UDP сервер для отправки данных регистрации клиентам"""

    def __init__(self, recorder, udp_port=9001, verbose=False):
        self.recorder = recorder
        self.udp_port = udp_port
        self.verbose = verbose
        self.clients = set()  # Множество адресов клиентов (ip, port)
        self.lock = threading.Lock()
        self.running = False
        self.recording_thread = None
        self.recording_active = False  # Флаг активности регистрации
        self._stream_sequence = 0
        self._stream_global_frame_idx = 0
        self._udp_send_queue = queue.Queue(maxsize=UDP_SEND_QUEUE_SIZE)
        self._udp_send_drops = 0
        self._stream_batch_drops = 0
        self._udp_sender_threads: list[threading.Thread] = []
        self._stream_packager_thread = None
        self._stream_batch_queue: queue.Queue | None = (
            queue.Queue(maxsize=STREAM_BATCH_QUEUE_SIZE) if STREAM_BATCH_QUEUE_SIZE > 0 else None
        )
        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sndbuf = 8 * 1024 * 1024 if INTAN_UDP_WIFI else 4 * 1024 * 1024
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        except OSError:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # Low delay
        self.sock.bind(('0.0.0.0', self.udp_port))
        self.sock.settimeout(1.0)  # Таймаут для возможности проверки running

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def start_listening(self):
        """Запускает поток для приема регистраций клиентов"""
        self.running = True
        alive = sum(1 for t in self._udp_sender_threads if t.is_alive())
        for i in range(alive, UDP_SENDER_THREADS):
            thread = threading.Thread(
                target=self._udp_sender_loop,
                daemon=True,
                name=f"udp-sender-{i}",
            )
            thread.start()
            self._udp_sender_threads.append(thread)
        listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        listener_thread.start()
        self._log(f"UDP сервер слушает на порту {self.udp_port}")

    def _listen_loop(self):
        """Цикл приема регистраций от клиентов"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                msg = data.decode('utf-8').strip()
                
                if msg == "REGISTER":
                    with self.lock:
                        self.clients.add(addr)
                    self.sock.sendto(b"REGISTERED", addr)
                    self._log(f"✓ Клиент зарегистрирован: {addr} (всего клиентов: {len(self.clients)})")
                elif msg == "UNREGISTER":
                    with self.lock:
                        self.clients.discard(addr)
                    self.sock.sendto(b"UNREGISTERED", addr)
                    self._log(f"Клиент отрегистрирован: {addr}")
                elif msg.startswith("START_RECORDING"):
                    # Формат: START_RECORDING channels sample_rate [duration]
                    parts = msg.split()
                    channels_str = parts[1] if len(parts) > 1 else "0-7"
                    sample_rate = int(parts[2]) if len(parts) > 2 else 40000
                    duration = float(parts[3]) if len(parts) > 3 else None

                    try:
                        self.start_recording(channels_str, sample_rate, duration)
                    except Exception as e:
                        self._log(f"Ошибка START_RECORDING: {e}")
                        self.sock.sendto(f"RECORDING_ERROR {e}".encode("utf-8"), addr)
                    else:
                        self.sock.sendto(b"RECORDING_STARTED", addr)
                elif msg == "STOP_RECORDING":
                    self.sock.sendto(b"RECORDING_STOPPED", addr)
                    self.stop_recording()
            except socket.timeout:
                continue
            except Exception as e:
                self._log(f"Ошибка в listen_loop: {e}")

    def start_recording(self, channels_str="0-7", sample_rate=40000, duration=None):
        """Запускает регистрацию и отправку данных"""
        # Проверяем, что recorder существует
        if not self.recorder:
            raise RuntimeError("recorder не инициализирован в UDPRecorderServer")
        
        # Парсим каналы
        channels = []
        for part in channels_str.split(','):
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                channels.extend(range(start, end + 1))
            else:
                channels.append(int(part))
        channels = sorted(list(set(channels)))
        
        # Intan RHS2116 поддерживает до 40 kSamples/s на канал (714 kSamples/s общая)
        max_rate_per_channel = 40000
        if sample_rate > max_rate_per_channel:
            self._log(f"⚠ Предупреждение: частота {sample_rate} Hz слишком высока, ограничиваем до {max_rate_per_channel} Hz на канал")
            sample_rate = max_rate_per_channel
        
        if len(channels) > 16:
            channels = channels[:16]
            self._log(f"⚠ Предупреждение: ограничиваем количество каналов до 16")

        usb_v2 = False
        if self.recorder.backend == "usb":
            self.recorder.ensure_initialized()
            usb_v2 = (
                _is_usb_backend(self.recorder.spi)
                and self.recorder.spi.firmware_version() == "v2"
            )
            sequential = (
                len(channels) > 1
                and channels == list(range(channels[0], channels[0] + len(channels)))
            )
            if usb_v2 and not (len(channels) == 1 or sequential):
                raise ValueError(
                    "USB V2 STM32 поддерживает real recording одного канала или "
                    f"непрерывного диапазона каналов; запрошено {channels}"
                )

        # Останавливаем предыдущую регистрацию только после валидации нового запроса.
        self.stop_recording()
        if self.recording_thread and self.recording_thread.is_alive():
            raise RuntimeError("предыдущая регистрация еще останавливается")
        
        # Вычисляем общую частоту ADC (частота на канал * количество каналов)
        # Это нужно для правильной настройки Register 0 (ADC buffer bias и MUX bias)
        num_channels = len(channels)
        total_adc_rate_ksps = (sample_rate * num_channels) / 1000.0  # в kS/s
        total_adc_rate_ksps = min(float(USB_V2_FIRMWARE_ADC_KSPS), total_adc_rate_ksps)
        
        # Переинициализируем чип с правильной частотой ADC, если нужно.
        # USB V2 *_REAL stream сам вызывает INIT_RECORD(350) в прошивке (TIM-slot DMA).
        if usb_v2:
            self._log(
                "USB V2: пропускаем ручной INIT_RECORD перед real stream; "
                f"прошивка установит {USB_V2_FIRMWARE_ADC_KSPS} kS/s"
            )
        elif self.recorder.initialized:
            # Проверяем, нужно ли переинициализировать с новой частотой
            # Если частота изменилась значительно, переинициализируем
            self._log(f"Настройка ADC для частоты {total_adc_rate_ksps:.1f} kS/s (каналов: {num_channels}, частота на канал: {sample_rate} Hz)")
            self.recorder._initialize_for_recording(verbose=self.recorder.verbose, adc_sampling_rate_ksps=total_adc_rate_ksps)
        else:
            # Первая инициализация - используем вычисленную частоту
            self.recorder.ensure_initialized()
            # Переинициализируем с правильной частотой
            self.recorder._initialize_for_recording(verbose=self.recorder.verbose, adc_sampling_rate_ksps=total_adc_rate_ksps)
        
        self._stream_sequence = 0
        self._stream_global_frame_idx = 0
        self._usb_stream8_ok = None
        self._usb_stream_range_ok = None
        self._udp_send_drops = 0
        self._stream_batch_drops = 0
        if self._stream_batch_queue is not None:
            while True:
                try:
                    self._stream_batch_queue.get_nowait()
                except queue.Empty:
                    break
            if self._stream_packager_thread is None or not self._stream_packager_thread.is_alive():
                self._stream_packager_thread = threading.Thread(
                    target=self._stream_packager_loop,
                    daemon=True,
                    name="udp-packager",
                )
                self._stream_packager_thread.start()
        self.recording_active = True
        self.recording_thread = threading.Thread(
            target=self._recording_loop,
            args=(channels, sample_rate, duration),
            daemon=True
        )
        self.recording_thread.start()
        self._log(f"Регистрация запущена: каналы {channels}, частота {sample_rate} Hz на канал (общая ADC: {total_adc_rate_ksps:.1f} kS/s)")

    def _recording_loop(self, channels, sample_rate, duration):
        """Цикл регистрации и отправки данных"""
        try:
            self._log(f"Запуск _recording_loop: каналы={channels}, частота={sample_rate} Hz, длительность={duration}")
            
            # Проверяем, что recorder инициализирован
            if not self.recorder:
                raise RuntimeError("recorder не инициализирован")
            
            clients_copy = []
            
            # Формат v2: [ver=2][sample_count] per sample: timestamp(8)+pipeline_skip(2)+ch_count(2)+ch_list(ch_count)+raw_count(2)+raw
            MAX_PACKET_SIZE = 1400
            channel_count = len(channels)
            raw_per_sample = 4 + channel_count + 2
            sample_size = 8 + 2 + 2 + channel_count + 2 + raw_per_sample * 2
            max_samples_per_packet = (MAX_PACKET_SIZE - 5) // sample_size
            max_samples_per_packet = max(1, min(max_samples_per_packet, 100))
            samples_buffer = []
            samples_buffer_append = samples_buffer.append
            
            print(f"[SERVER] 📖 Начало чтения данных из record_channels...")
            self._log(f"Начало чтения данных из record_channels...")
            sample_iter = iter(self.recorder.record_channels(channels, sample_rate, duration))
            sample_count = 0
            frame_dt = (1.0 / float(sample_rate)) if sample_rate > 0 else 0.0
            ch_bytes = bytes(ch & 0xFF for ch in channels)
            
            while True:
                if not self.recording_active:
                    self._log("Регистрация остановлена по запросу")
                    break

                try:
                    item = next(sample_iter)
                    
                    if not self.recording_active:
                        self._log("Регистрация остановлена по запросу")
                        break
                    
                    if len(clients_copy) == 0:
                        with self.lock:
                            clients_copy = list(self.clients)
                    elif len(self.clients) != len(clients_copy):
                        with self.lock:
                            clients_copy = list(self.clients)
                    
                    if isinstance(item, StreamBatch):
                        if not clients_copy:
                            continue
                        payload = item.payload
                        frame_bytes = channel_count * 2
                        n_frames = len(payload) // frame_bytes
                        if n_frames <= 0:
                            continue
                        if sample_count == 0:
                            self._log(
                                f"✓ Первый USB batch (v3): {n_frames} кадров × {channel_count} каналов, "
                                f"{len(payload)} байт"
                            )
                        sample_count += n_frames
                        if self._stream_batch_queue is not None:
                            try:
                                self._stream_batch_queue.put_nowait(
                                    (item, tuple(clients_copy), channel_count, frame_dt)
                                )
                            except queue.Full:
                                self._stream_batch_drops += 1
                        else:
                            self._send_usb_batch_udp(
                                item,
                                clients_copy,
                                channel_count,
                                frame_dt,
                            )
                        continue
                    
                    timestamp, raw_values, ch_list, pipeline_skip = item
                    sample_count += 1
                    
                    if sample_count == 1:
                        self._log(f"✓ Первый sample: timestamp={timestamp:.6f}, raw={len(raw_values)}, pipeline_skip={pipeline_skip}")
                    
                    if not clients_copy:
                        if sample_count <= 10:
                            self._log(f"⚠ Нет зарегистрированных клиентов (sample #{sample_count})")
                        continue
                    
                    samples_buffer_append((timestamp, raw_values, ch_list, pipeline_skip))
                    
                    if len(samples_buffer) >= max_samples_per_packet:
                        self._send_packet(samples_buffer, clients_copy, ch_bytes)
                        samples_buffer = []
                        samples_buffer_append = samples_buffer.append
                        
                except StopIteration:
                    # Итератор завершился (нормальное завершение)
                    self._log(f"Итератор record_channels завершился. Всего samples: {sample_count}")
                    break
                except Exception as e:
                    import traceback
                    self._log(f"❌ Ошибка при чтении sample #{sample_count + 1}: {e}")
                    self._log(f"Детали: {traceback.format_exc()}")
                    raise
                
            # Отправляем оставшиеся samples
            if samples_buffer:
                self._send_packet(samples_buffer, clients_copy, ch_bytes)
                
            self._log(f"✓ Регистрация завершена успешно. Всего samples: {sample_count}")
            if self._udp_send_drops or self._stream_batch_drops:
                print(
                    f"[UDP] ⚠ drops: udp={self._udp_send_drops} "
                    f"batch={self._stream_batch_drops}"
                )
                self._log(
                    f"⚠ UDP drops: пакеты={self._udp_send_drops}, "
                    f"usb_batch={self._stream_batch_drops}"
                )
                
        except Exception as e:
            import traceback
            self._log(f"❌ Критическая ошибка в recording_loop: {e}")
            self._log(f"Детали: {traceback.format_exc()}")
        finally:
            self.recording_active = False
            self._log("Цикл регистрации завершен")
    
    def _stream_packager_loop(self):
        """USB batch → UDP пакеты в отдельном потоке (не блокирует чтение USB)."""
        q = self._stream_batch_queue
        if q is None:
            return
        while self.running:
            try:
                item, clients_copy, channel_count, frame_dt = q.get(timeout=0.5)
            except queue.Empty:
                if not self.recording_active:
                    break
                continue
            if not self.recording_active and item is None:
                break
            self._send_usb_batch_udp(item, list(clients_copy), channel_count, frame_dt)

    def _send_usb_batch_udp(self, batch, clients_copy, channel_count, frame_dt):
        """UDP v4 (компактный) или v3 + очередь отправки (не блокирует USB)."""
        payload = batch.payload
        frame_bytes = channel_count * 2
        n_frames = len(payload) // frame_bytes
        if n_frames <= 0:
            return

        use_v4 = (
            channel_count > 0
            and batch.channels == list(range(batch.channels[0], batch.channels[0] + channel_count))
        )
        use_v6 = (
            use_v4
            and INTAN_UDP_USE_V6
            and getattr(batch, "capture_ts_ns", 0) > 0
        )
        use_v5 = use_v4 and INTAN_UDP_USE_V5 and not use_v6
        header_size = (
            V6_HEADER_SIZE
            if use_v6
            else (V5_HEADER_SIZE if use_v5 else (V4_HEADER_SIZE if use_v4 else V3_HEADER_SIZE))
        )
        max_frames_per_packet = (MAX_UDP_DATAGRAM - header_size) // frame_bytes
        max_frames_per_packet = max(1, max_frames_per_packet)

        batch_ts_ns = int(batch.timestamp * 1_000_000_000)
        frame_dt_ns = int(frame_dt * 1_000_000_000) if frame_dt > 0 else 0
        ch_template = (
            V3_CHANNEL_TEMPLATE
            if channel_count == 16
            else bytes(ch & 0xFF for ch in batch.channels)
        )

        packets: list[bytes] = []
        clients_tuple = tuple(clients_copy)

        for start in range(0, n_frames, max_frames_per_packet):
            chunk_frames = min(max_frames_per_packet, n_frames - start)
            chunk_len = chunk_frames * frame_bytes
            packet = bytearray(header_size + chunk_len)
            seq = self._stream_sequence
            self._stream_sequence += 1

            if use_v6:
                mux_count = max(1, int(getattr(batch, "stream_mux_count", 0) or channel_count))
                adc_rate_hz = int(getattr(batch, "adc_rate_hz", 0) or USB_V2_FIRMWARE_ADC_HZ)
                capture_ts_ns = int(batch.capture_ts_ns)
                per_ch_hz = adc_rate_hz / mux_count if mux_count > 0 else adc_rate_hz
                if start > 0 and per_ch_hz > 0:
                    capture_ts_ns += int(start * 1_000_000_000 / per_ch_hz)
                struct.pack_into(
                    "<IIHH",
                    packet,
                    0,
                    INTAN_STREAM_V6_MAGIC,
                    seq,
                    channel_count,
                    chunk_frames,
                )
                struct.pack_into("<I", packet, 12, self._stream_global_frame_idx)
                struct.pack_into(
                    "<I",
                    packet,
                    16,
                    batch.hw_counter + start * channel_count,
                )
                struct.pack_into("<Q", packet, 20, capture_ts_ns)
                struct.pack_into("<I", packet, 28, adc_rate_hz)
                self._stream_global_frame_idx += chunk_frames
            elif use_v5:
                struct.pack_into(
                    "<IIHH",
                    packet,
                    0,
                    INTAN_STREAM_V5_MAGIC,
                    seq,
                    channel_count,
                    chunk_frames,
                )
                struct.pack_into("<I", packet, 12, self._stream_global_frame_idx)
                struct.pack_into(
                    "<I",
                    packet,
                    16,
                    batch.hw_counter + start * channel_count,
                )
                self._stream_global_frame_idx += chunk_frames
            elif use_v4:
                struct.pack_into(
                    "<IIHH",
                    packet,
                    0,
                    INTAN_STREAM_V4_MAGIC,
                    seq,
                    channel_count,
                    chunk_frames,
                )
                struct.pack_into("<I", packet, 12, self._stream_global_frame_idx)
                self._stream_global_frame_idx += chunk_frames
            else:
                ts_ns = batch_ts_ns + start * frame_dt_ns
                struct.pack_into(
                    "<IHHIQHHHH",
                    packet,
                    0,
                    INTAN_STREAM_MAGIC,
                    INTAN_STREAM_VERSION,
                    V3_HEADER_SIZE,
                    seq,
                    ts_ns,
                    channel_count,
                    chunk_frames,
                    0,
                    0,
                )
                packet[28 : 28 + channel_count] = ch_template[:channel_count]
                if channel_count < 16:
                    packet[28 + channel_count : 44] = b"\x00" * (16 - channel_count)

            packet[header_size:] = payload[
                start * frame_bytes : start * frame_bytes + chunk_len
            ]
            packets.append(bytes(packet))

        if packets:
            self._enqueue_udp_packets(packets, clients_tuple)

    def _enqueue_udp_packet(self, packet, clients_copy):
        """Постановка одного пакета в очередь отправки."""
        if not clients_copy:
            return
        self._enqueue_udp_packets(
            [bytes(packet) if not isinstance(packet, bytes) else packet],
            tuple(clients_copy),
        )

    def _enqueue_udp_packets(self, packets: list[bytes], clients_tuple: tuple):
        """Пакетная постановка в очередь — меньше накладных на Wi‑Fi."""
        if not packets or not clients_tuple:
            return
        try:
            self._udp_send_queue.put_nowait((packets, clients_tuple))
        except queue.Full:
            self._udp_send_drops += len(packets)

    def _udp_sender_loop(self):
        """Фоновая отправка UDP — разгружает цикл регистрации."""
        while self.running:
            try:
                batch = [self._udp_send_queue.get(timeout=0.5)]
            except queue.Empty:
                continue
            for _ in range(UDP_SENDER_DRAIN_BATCHES - 1):
                try:
                    batch.append(self._udp_send_queue.get_nowait())
                except queue.Empty:
                    break
            for packets, clients_copy in batch:
                for packet in packets:
                    self._send_raw_packet_nowait(packet, clients_copy)

    def _send_raw_packet(self, packet, clients_copy):
        self._enqueue_udp_packet(packet, clients_copy)

    def _send_raw_packet_nowait(self, packet, clients_copy):
        """Непосредственная отправка (только из потока udp-sender)."""
        failed_clients = []
        for client_addr in clients_copy:
            try:
                self.sock.sendto(packet, client_addr)
            except (socket.error, OSError) as e:
                failed_clients.append(client_addr)
                if self.verbose:
                    self._log(f"⚠ Ошибка отправки пакета клиенту {client_addr}: {e}")

        if failed_clients:
            with self.lock:
                for client_addr in failed_clients:
                    self.clients.discard(client_addr)

    def _send_packet(self, samples_buffer, clients_copy, ch_bytes=None):
        """Отправляет пакет: формат с pipeline (1 tx/convert), смещение обрабатывается в GUI"""
        if not samples_buffer or not clients_copy:
            return
        
        sample_count = len(samples_buffer)
        packet = bytearray(5)
        packet[0] = 2  # версия формата: 2 = pipeline (1 tx/convert), смещение в GUI
        struct.pack_into('I', packet, 1, sample_count)
        
        for timestamp, raw_values, ch_list, pipeline_skip in samples_buffer:
            raw_count = len(raw_values)
            ch_count = len(ch_list)
            base = 8 + 2 + 2
            block_size = base + ch_count + 2 + raw_count * 2
            sample_block = bytearray(block_size)
            struct.pack_into('dHH', sample_block, 0, timestamp, pipeline_skip, ch_count)
            if ch_bytes is not None and len(ch_bytes) == ch_count:
                sample_block[base : base + ch_count] = ch_bytes
            else:
                for i, ch in enumerate(ch_list):
                    sample_block[base + i] = ch & 0xFF
            struct.pack_into('H', sample_block, base + ch_count, raw_count)
            off = base + ch_count + 2
            for i, v in enumerate(raw_values):
                struct.pack_into('<H', sample_block, off + i * 2, v & 0xFFFF)
            packet += sample_block
        
        self._send_raw_packet(packet, clients_copy)

    def stop_recording(self):
        """Останавливает регистрацию"""
        if self.recording_active:
            self._log("Остановка регистрации...")
            self.recording_active = False
            try:
                spi = getattr(self.recorder, "spi", None)
                if _is_usb_backend(spi) and hasattr(spi, "stop_stream"):
                    spi.stop_stream()
            except Exception as e:
                self._log(f"⚠ Не удалось сразу остановить USB stream: {e}")
            # Ждем завершения потока (максимум 2 секунды)
            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=2.0)
                if self.recording_thread.is_alive():
                    self._log("⚠ Поток регистрации не завершился в течение 2 секунд")
            if self._stream_batch_queue is not None:
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if self._stream_batch_queue.empty():
                        break
                    time.sleep(0.05)

    def get_clients_count(self):
        """Возвращает количество зарегистрированных клиентов"""
        with self.lock:
            return len(self.clients)

    def stop(self):
        """Останавливает сервер"""
        self.running = False
        self.sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="UDP-сервер для регистрации данных Intan RHS2116"
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=9001,
        help="UDP порт для отправки данных (по умолчанию: 9001)"
    )
    parser.add_argument(
        "-g", "--gpio",
        type=int,
        default=226,
        help="Номер GPIO для PH2 (по умолчанию: 226)"
    )
    parser.add_argument(
        "-d", "--device",
        default="/dev/spidev1.1",
        help="Путь к SPI устройству (по умолчанию: /dev/spidev1.1)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Подробный вывод"
    )

    args = parser.parse_args()

    recorder = IntanRecorder(
        gpio_number=args.gpio,
        spi_device=args.device,
        verbose=args.verbose
    )

    server = UDPRecorderServer(recorder, udp_port=args.udp_port, verbose=args.verbose)
    server.start_listening()

    try:
        print("=" * 60)
        print("UDP-сервер регистрации Intan RHS2116 запущен")
        print("=" * 60)
        print(f"UDP порт: {args.udp_port}")
        print(f"GPIO PH2: {args.gpio}")
        print(f"SPI устройство: {args.device}")
        print("\nКоманды клиентов:")
        print("  REGISTER - зарегистрироваться для получения данных")
        print("  UNREGISTER - отменить регистрацию")
        print("  START_RECORDING <channels> <sample_rate> [duration] - начать регистрацию")
        print("  STOP_RECORDING - остановить регистрацию")
        print("=" * 60)
        print("\nНажмите Ctrl+C для остановки\n")

        while True:
            time.sleep(1)
            clients_count = server.get_clients_count()
            if clients_count > 0:
                print(f"\rАктивных клиентов: {clients_count}", end='', flush=True)
    except KeyboardInterrupt:
        print("\n\nОстановка сервера...")
    finally:
        server.stop()
        print("Сервер остановлен")


if __name__ == "__main__":
    main()

