#!/usr/bin/env python3
"""
Общий runtime-state для координации TCP-стимуляции и UDP-регистрации.

Идея простая:
- один chip_lock сериализует доступ к SPI/режимам чипа;
- recording-конфигурация хранится централизованно, чтобы после стимуляции
  можно было вернуть чип в recording mode с прежними параметрами;
- статусные события рассылаются подписчикам (например, UDP-серверу для GUI).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional


class IntanSharedState:
    """Координирует совместную работу TCP и UDP поверх одного Intan-чипа."""

    def __init__(self):
        self.chip_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._event_listeners: List[Callable[[Dict[str, object]], None]] = []

        self.recording_active = False
        self.recording_channels: List[int] = []
        self.recording_sample_rate_hz: Optional[int] = None
        self.recording_duration_s: Optional[float] = None
        self.recording_adc_rate_ksps: float = 480.0
        self.recording_started_at: Optional[float] = None

        self.stim_active = False
        self.last_operation: Optional[str] = None

    def add_event_listener(self, callback: Callable[[Dict[str, object]], None]) -> None:
        with self._state_lock:
            self._event_listeners.append(callback)

    def emit_event(self, event: str, **payload) -> None:
        message = {
            "event": str(event),
            "ts": time.time(),
            **payload,
        }
        with self._state_lock:
            listeners = list(self._event_listeners)
        for callback in listeners:
            try:
                callback(message)
            except Exception:
                # Статусные события не должны ломать рабочий поток.
                continue

    def set_recording_session(self, channels, sample_rate_hz, duration_s, adc_rate_ksps) -> None:
        with self._state_changed:
            self.recording_active = True
            self.recording_channels = list(channels)
            self.recording_sample_rate_hz = int(sample_rate_hz)
            self.recording_duration_s = duration_s
            self.recording_adc_rate_ksps = float(adc_rate_ksps)
            self.recording_started_at = time.time()
            self._state_changed.notify_all()

    def clear_recording_session(self) -> None:
        with self._state_changed:
            self.recording_active = False
            self.recording_channels = []
            self.recording_sample_rate_hz = None
            self.recording_duration_s = None
            self.recording_started_at = None
            self._state_changed.notify_all()

    def snapshot_recording_session(self) -> Dict[str, object]:
        with self._state_lock:
            return {
                "recording_active": bool(self.recording_active),
                "channels": list(self.recording_channels),
                "sample_rate_hz": self.recording_sample_rate_hz,
                "duration_s": self.recording_duration_s,
                "adc_rate_ksps": float(self.recording_adc_rate_ksps),
                "started_at": self.recording_started_at,
            }

    def begin_stimulation(self, operation: str) -> Dict[str, object]:
        with self._state_changed:
            snapshot = self.snapshot_recording_session()
            self.stim_active = True
            self.last_operation = operation
            self._state_changed.notify_all()
        self.emit_event(
            "stim_started",
            operation=operation,
            recording_active=bool(snapshot.get("recording_active")),
        )
        return snapshot

    def end_stimulation(self, operation: str, restored_recording: bool = False) -> None:
        with self._state_changed:
            self.stim_active = False
            self.last_operation = operation
            self._state_changed.notify_all()
        self.emit_event(
            "stim_finished",
            operation=operation,
            restored_recording=bool(restored_recording),
        )

    def wait_until_recording_allowed(self, timeout_s: float = 0.1) -> None:
        with self._state_changed:
            while self.stim_active:
                self._state_changed.wait(timeout=timeout_s)
