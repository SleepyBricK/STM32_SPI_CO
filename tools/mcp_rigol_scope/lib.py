"""Rigol DHO/DS/MSO — SCPI через PyVISA (USB)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

RIGOL_VID = 0x1AB1
DEFAULT_TIMEOUT_MS = 15_000
DHO804_RESOURCE = "USB0::6833::1101::DHO8A272405662::0::INSTR"
TRIG_STOP_STATUSES = {"STOP"}


@dataclass
class RigolIdentity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    resource: str

    @classmethod
    def from_idn(cls, resource: str, idn: str) -> RigolIdentity:
        parts = [p.strip() for p in idn.split(",")]
        while len(parts) < 4:
            parts.append("")
        return cls(parts[0], parts[1], parts[2], parts[3], resource)


def _resource_manager():
    import pyvisa

    return pyvisa.ResourceManager("@py")


def list_resources() -> list[str]:
    return list(_resource_manager().list_resources())


def find_rigol_resource(preferred: str | None = None) -> str | None:
    if preferred:
        return preferred
    for resource in list_resources():
        upper = resource.upper()
        if "USB" not in upper:
            continue
        if str(RIGOL_VID) in resource or "1AB1" in upper or "6833" in resource:
            return resource
    for resource in list_resources():
        if resource.startswith("USB"):
            try:
                with open_instrument(resource, timeout_ms=3000) as inst:
                    idn = inst.query("*IDN?").strip().upper()
                    if "RIGOL" in idn:
                        return resource
            except Exception:
                continue
    return None


class RigolScope:
    def __init__(self, resource: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._inst = None

    def __enter__(self) -> RigolScope:
        rm = _resource_manager()
        self._inst = rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        self._inst.chunk_size = 4 * 1024 * 1024
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._inst is not None:
            self._inst.close()
            self._inst = None

    def query(self, cmd: str) -> str:
        assert self._inst is not None
        return self._inst.query(cmd).strip()

    def write(self, cmd: str) -> None:
        assert self._inst is not None
        self._inst.write(cmd)

    def idn(self) -> RigolIdentity:
        return RigolIdentity.from_idn(self.resource, self.query("*IDN?"))

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def single(self) -> None:
        self.write(":SING")

    def trigger_status(self) -> str | None:
        status = _safe_query(self, ":TRIG:STAT?")
        return _normalize_trigger_status(status)

    def _safe_write(self, cmd: str) -> bool:
        try:
            self.write(cmd)
            return True
        except Exception:
            return False

    def _set_trigger_level(self, channel: int, level_v: float) -> None:
        idn = _safe_query(self, "*IDN?") or ""
        if "DHO" in idn.upper():
            self.write(f":TRIG:LEV:CH{channel} {level_v}")
        else:
            self.write(f":TRIG:LEV {level_v}")

    def status(self) -> dict[str, Any]:
        chans: dict[str, dict[str, Any]] = {}
        for n in (1, 2, 3, 4):
            prefix = f":CHAN{n}"
            try:
                if self.query(f"{prefix}:DISP?") != "1":
                    continue
            except Exception:
                continue
            chans[f"CH{n}"] = {
                "scale_v": float(self.query(f"{prefix}:SCAL?")),
                "offset_v": float(self.query(f"{prefix}:OFFS?")),
                "coupling": self.query(f"{prefix}:COUP?"),
                "impedance": self.query(f"{prefix}:IMP?"),
            }
        trig: dict[str, Any] = {"mode": self.query(":TRIG:MODE?")}
        try:
            trig["source"] = self.query(":TRIG:EDGE:SOUR?")
            trig["slope"] = self.query(":TRIG:EDGE:SLOP?")
            trig["sweep"] = self.query(":TRIG:SWE?")
        except Exception:
            pass
        return {
            "resource": self.resource,
            "idn": self.idn().__dict__,
            "timebase": {
                "scale_s": float(self.query(":TIM:SCAL?")),
                "offset_s": float(self.query(":TIM:OFFS?")),
            },
            "acq_mode": _safe_query(self, ":ACQ:MODE?"),
            "trigger": trig,
            "channels": chans,
        }

    def configure(
        self,
        *,
        channel: int = 1,
        scale_v: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        time_scale_s: float | None = None,
        time_offset_s: float | None = None,
        trigger_mode: str | None = None,
        trigger_source: str | None = None,
        trigger_level_v: float | None = None,
        trigger_slope: str | None = None,
        trigger_sweep: str | None = None,
    ) -> dict[str, Any]:
        self.stop()
        self._safe_write("*CLS")
        ch = f"CHAN{channel}"
        self.write(f":{ch}:DISP ON")
        if scale_v is not None:
            self.write(f":{ch}:SCAL {scale_v}")
        if offset_v is not None:
            self.write(f":{ch}:OFFS {offset_v}")
        if coupling is not None:
            self.write(f":{ch}:COUP {coupling.upper()}")
        if time_scale_s is not None:
            self.write(f":TIM:SCAL {time_scale_s}")
        if time_offset_s is not None:
            self.write(f":TIM:OFFS {time_offset_s}")
        if trigger_mode is not None:
            self.write(f":TRIG:MODE {trigger_mode.upper()}")
        if trigger_source is not None:
            src = trigger_source.upper()
            if not src.startswith("CHAN"):
                src = f"CHAN{src.replace('CH', '')}"
            self.write(f":TRIG:EDGE:SOUR {src}")
        if trigger_level_v is not None:
            self._set_trigger_level(channel, trigger_level_v)
        if trigger_slope is not None:
            self.write(f":TRIG:EDGE:SLOP {trigger_slope.upper()}")
        if trigger_sweep is not None:
            self.write(f":TRIG:SWE {trigger_sweep.upper()}")
        self.wait_opc(timeout_s=1.0)
        return {"ok": True}

    def configure_stim(
        self,
        *,
        channel: int = 1,
        scale_v: float = 0.5,
        offset_v: float = 0.0,
        coupling: str = "DC",
        time_scale_s: float = 1e-3,
        time_offset_s: float = 0.0,
        trigger_level_v: float = 0.2,
        trigger_slope: str = "POS",
        trigger_sweep: str = "AUTO",
    ) -> dict[str, Any]:
        self.stop()
        self._safe_write(":ACQ:MODE NORM")
        self._safe_write(":ACQ:TYPE NORM")
        return self.configure(
            channel=channel,
            scale_v=scale_v,
            offset_v=offset_v,
            coupling=coupling,
            time_scale_s=time_scale_s,
            time_offset_s=time_offset_s,
            trigger_mode="EDGE",
            trigger_source=f"CHAN{channel}",
            trigger_level_v=trigger_level_v,
            trigger_slope=trigger_slope,
            trigger_sweep=trigger_sweep,
        )

    def arm_single(self) -> None:
        self.stop()
        self._safe_write("*CLS")
        self.single()

    def arm_run(self) -> None:
        self._safe_write("*CLS")
        self.run()

    def wait_opc(self, timeout_s: float = 5.0) -> bool:
        assert self._inst is not None
        old_timeout = self._inst.timeout
        self._inst.timeout = max(1, int(timeout_s * 1000))
        try:
            return self.query("*OPC?") == "1"
        except Exception:
            return False
        finally:
            self._inst.timeout = old_timeout

    def wait_acq_complete(self, timeout_s: float = 5.0, poll_s: float = 0.05) -> dict[str, Any]:
        t0 = time.perf_counter()
        deadline = t0 + timeout_s
        last_status: str | None = None
        status_supported = True

        while time.perf_counter() < deadline:
            try:
                last_status = _normalize_trigger_status(self.query(":TRIG:STAT?"))
            except Exception:
                status_supported = False
                break
            if last_status in TRIG_STOP_STATUSES:
                return {
                    "complete": True,
                    "method": "TRIG:STAT",
                    "status": last_status,
                    "elapsed_s": time.perf_counter() - t0,
                }
            time.sleep(poll_s)

        if (not status_supported) or last_status in {"TD", "TRIG"}:
            remaining = max(0.1, deadline - time.perf_counter())
            if self.wait_opc(remaining):
                return {
                    "complete": True,
                    "method": "*OPC?",
                    "status": last_status,
                    "elapsed_s": time.perf_counter() - t0,
                }

        raise TimeoutError(f"Rigol acquisition did not complete in {timeout_s:.3f}s; status={last_status!r}")

    def read_waveform(
        self,
        *,
        channel: int = 1,
        points: int = 1000,
        mode: str = "NORM",
    ) -> tuple[np.ndarray, np.ndarray]:
        ch = f"CHAN{channel}"
        assert self._inst is not None
        self.write(f":WAV:SOUR {ch}")
        self.write(f":WAV:MODE {mode.upper()}")
        self.write(":WAV:FORM BYTE")
        self.write(f":WAV:POIN {int(points)}")
        self._safe_write(":WAV:STAR 1")
        self._safe_write(f":WAV:STOP {int(points)}")

        pre = self.query(":WAV:PRE?")
        _, _, _, _, xinc, xorig, _, yinc, yorig, yref = [float(x) for x in pre.split(",")]

        self._inst.write(":WAV:DATA?")  # type: ignore[union-attr]
        raw = self._inst.read_raw()  # type: ignore[union-attr]
        payload = _parse_binary_block(raw)
        codes = np.frombuffer(payload, dtype=np.uint8)
        volts = (codes.astype(np.float64) - yref) * yinc + yorig
        times = xorig + np.arange(len(volts), dtype=np.float64) * xinc
        return times, volts

    def capture(
        self,
        *,
        channel: int = 1,
        points: int = 1000,
        wait_s: float = 0.5,
        single: bool = False,
        waveform_mode: str = "NORM",
    ) -> tuple[np.ndarray, np.ndarray]:
        if single:
            self.arm_single()
            self.wait_acq_complete(timeout_s=wait_s)
        else:
            self.arm_run()
            time.sleep(max(wait_s, 0.05))
            self.stop()
            self.wait_opc(timeout_s=2.0)
        return self.read_waveform(channel=channel, points=points, mode=waveform_mode)

    def capture_during_pattern_run(
        self,
        run_fn: Callable[[], Any],
        *,
        channel: int = 1,
        points: int = 8000,
        timeout_s: float = 10.0,
        arm: str = "run",
        arm_settle_s: float = 0.05,
        poll_s: float = 0.05,
        run_join_timeout_s: float | None = None,
        waveform_mode: str = "NORM",
        score_expected_v: float | None = None,
    ) -> tuple[np.ndarray, np.ndarray, Any]:
        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        def runner() -> None:
            try:
                result["value"] = run_fn()
            except BaseException as exc:  # propagate after scope read/cleanup
                errors.append(exc)

        arm_mode = arm.lower()
        if arm_mode == "single":
            # DHO804 keeps TRIG:STAT=STOP even while armed; RUN+polling is reliable for stim.
            self.arm_run()
        elif arm_mode in {"run", "normal"}:
            self.arm_run()
        else:
            raise ValueError(f"unsupported arm mode: {arm!r}")

        time.sleep(max(0.0, arm_settle_s))
        th = threading.Thread(target=runner, daemon=True)
        th.start()

        join_timeout = run_join_timeout_s if run_join_timeout_s is not None else timeout_s
        deadline = time.perf_counter() + max(0.0, join_timeout)
        best: tuple[float, np.ndarray, np.ndarray] | None = None

        def frame_score(volts: np.ndarray) -> float:
            if score_expected_v is not None:
                pl = measure_plateau_v(volts, expected_v=score_expected_v)
                score = float(pl["v_plateau_v"])
                if not np.isnan(score):
                    return score
            return float(np.max(volts)) if len(volts) else float("-inf")

        try:
            while th.is_alive() and time.perf_counter() < deadline:
                t, v = self.read_waveform(channel=channel, points=points, mode=waveform_mode)
                if len(v) == 0:
                    time.sleep(max(0.0, poll_s))
                    continue
                score = frame_score(v)
                if best is None or score > best[0]:
                    best = (score, t, v)
                time.sleep(max(0.0, poll_s))

            th.join(timeout=max(0.0, deadline - time.perf_counter()))
            if th.is_alive():
                raise TimeoutError("pattern callback did not finish before Rigol capture deadline")

            t, v = self.read_waveform(channel=channel, points=points, mode=waveform_mode)
            if len(v) > 0:
                score = frame_score(v)
                if best is None or score > best[0]:
                    best = (score, t, v)
            if best is None:
                raise RuntimeError("Rigol capture returned no waveform samples")
            times, volts = best[1], best[2]
        except BaseException:
            self.stop()
            raise
        else:
            self.stop()

        if errors:
            raise errors[0]
        return times, volts, result.get("value")


def open_instrument(resource: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> RigolScope:
    return RigolScope(resource, timeout_ms=timeout_ms)


def _safe_query(scope: RigolScope, cmd: str) -> str | None:
    try:
        return scope.query(cmd)
    except Exception:
        return None


def _normalize_trigger_status(status: str | None) -> str | None:
    if status is None:
        return None
    return status.strip().upper().replace("'", "")


def _parse_binary_block(raw: bytes) -> bytes:
    if not raw or raw[0:1] != b"#":
        return raw
    nd = int(chr(raw[1]))
    n = int(raw[2 : 2 + nd].decode())
    return raw[2 + nd : 2 + nd + n]


def scan_rigol() -> dict[str, Any]:
    resources = list_resources()
    devices: list[dict[str, Any]] = []
    for resource in resources:
        if not resource.startswith("USB"):
            continue
        try:
            with open_instrument(resource, timeout_ms=5000) as scope:
                ident = scope.idn()
                if "RIGOL" not in ident.manufacturer.upper():
                    continue
                devices.append(
                    {
                        "resource": resource,
                        "idn": ident.__dict__,
                        "recommended": True,
                    }
                )
        except Exception as exc:
            devices.append({"resource": resource, "error": str(exc)})
    default = devices[0]["resource"] if devices and "idn" in devices[0] else None
    return {
        "resources": resources,
        "rigol": devices,
        "default_resource": default,
    }


def capture_summary(
    resource: str | None = None,
    *,
    channel: int = 1,
    points: int = 1000,
    wait_s: float = 0.5,
    single: bool = False,
    save_csv: str | None = None,
) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден по USB/VISA. Проверьте кабель и закройте Rigol UltraScope/Station."}
    with open_instrument(res) as scope:
        t, v = scope.capture(channel=channel, points=points, wait_s=wait_s, single=single)
    summary = {
        "ok": True,
        "resource": res,
        "channel": channel,
        "points": int(len(v)),
        "dt_s": float(t[1] - t[0]) if len(t) > 1 else None,
        "t_start_s": float(t[0]),
        "t_end_s": float(t[-1]),
        "v_min": float(np.min(v)),
        "v_max": float(np.max(v)),
        "v_pp": float(np.max(v) - np.min(v)),
        "v_mean": float(np.mean(v)),
    }
    if save_csv:
        path = Path(save_csv).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savetxt(path, np.column_stack([t, v]), delimiter=",", header="time_s,voltage_v", comments="")
        summary["csv"] = str(path)
    return summary


def measure_plateau_v(
    volts: np.ndarray,
    *,
    expected_v: float | None = None,
    edge_frac: float = 0.15,
) -> dict[str, float | int]:
    """Upper level of rectangular pulses: median of plateaus, edges excluded."""
    v = np.asarray(volts, dtype=np.float64)
    if len(v) == 0:
        return {"v_plateau_v": float("nan"), "v_peak_v": float("nan"), "n_plateau": 0}

    baseline = float(np.percentile(v, 20))
    v_peak = float(np.max(v))
    span = v_peak - baseline
    if span < 0.01:
        return {"v_plateau_v": baseline, "v_peak_v": v_peak, "n_plateau": 0}

    thr = baseline + 0.5 * span
    if expected_v is not None and expected_v > 0:
        thr = max(thr, expected_v * 0.35)

    high = v > thr
    if not np.any(high):
        return {"v_plateau_v": float("nan"), "v_peak_v": v_peak, "n_plateau": 0}

    edges = np.diff(high.astype(np.int8))
    rises = list(np.where(edges == 1)[0] + 1)
    falls = list(np.where(edges == -1)[0] + 1)
    if high[0]:
        rises.insert(0, 0)
    if high[-1]:
        falls.append(len(v))

    interior: list[float] = []
    for r, f in zip(rises, falls):
        seg = v[r:f]
        n = len(seg)
        if n <= 2:
            interior.extend(float(x) for x in seg)
            continue
        skip = max(1, int(n * edge_frac))
        if n > 2 * skip:
            interior.extend(float(x) for x in seg[skip:-skip])
        else:
            interior.append(float(np.median(seg)))

    plateau = float(np.median(interior)) if interior else float("nan")
    return {"v_plateau_v": plateau, "v_peak_v": v_peak, "n_plateau": len(interior)}


def measure_high_pulses(
    time_s: np.ndarray,
    volts: np.ndarray,
    threshold_v: float,
) -> list[float]:
    high = volts > threshold_v
    if not np.any(high):
        return []
    edges = np.diff(high.astype(np.int8))
    rises = list(np.where(edges == 1)[0] + 1)
    falls = list(np.where(edges == -1)[0] + 1)
    if high[0]:
        rises.insert(0, 0)
    if high[-1]:
        falls.append(len(high) - 1)
    widths: list[float] = []
    for i in range(min(len(rises), len(falls))):
        if falls[i] > rises[i]:
            widths.append(float(time_s[falls[i]] - time_s[rises[i]]))
    return widths


def _pulse_summary(
    t: np.ndarray,
    v: np.ndarray,
    threshold_v: float | None,
) -> dict[str, Any]:
    v_min = float(np.min(v))
    v_max = float(np.max(v))
    v_med = float(np.median(v))
    v_pp = v_max - v_min
    thr = threshold_v
    if thr is None:
        thr = v_med + 0.45 * v_pp if v_pp > 0.01 else max(0.05, v_max * 0.3)
    widths_s = measure_high_pulses(t, v, thr)
    plateau = measure_plateau_v(v, expected_v=threshold_v if threshold_v and threshold_v > 0.1 else None)
    return {
        "points": int(len(v)),
        "dt_s": float(t[1] - t[0]) if len(t) > 1 else None,
        "t_start_s": float(t[0]) if len(t) else None,
        "t_end_s": float(t[-1]) if len(t) else None,
        "threshold_v": float(thr),
        "v_min_v": v_min,
        "v_max_v": v_max,
        "v_plateau_v": float(plateau["v_plateau_v"]),
        "v_peak_v": float(plateau["v_peak_v"]),
        "plateau_samples": int(plateau["n_plateau"]),
        "v_median_v": v_med,
        "v_pp_v": v_pp,
        "pulse_count": len(widths_s),
        "widths_us": [w * 1e6 for w in widths_s],
        "widths_s": widths_s,
    }


def measure_pulses(
    resource: str | None = None,
    *,
    channel: int = 1,
    points: int = 4000,
    threshold_v: float | None = None,
    wait_s: float = 2.0,
    single: bool = True,
) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        t, v = scope.capture(channel=channel, points=points, wait_s=wait_s, single=single)
    return {
        "ok": True,
        "resource": res,
        "channel": channel,
        **_pulse_summary(t, v, threshold_v),
    }


def measure_pulses_synced(
    resource: str | None,
    run_fn: Callable[[], Any],
    *,
    channel: int = 1,
    points: int = 8000,
    threshold_v: float | None = None,
    scale_v: float = 0.5,
    offset_v: float = 0.0,
    time_scale_s: float = 1e-3,
    time_offset_s: float = 0.0,
    trigger_level_v: float = 0.2,
    trigger_sweep: str = "AUTO",
    timeout_s: float = 10.0,
    run_join_timeout_s: float | None = None,
    save_csv: str | None = None,
    return_waveform: bool = False,
    expected_plateau_v: float | None = None,
) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        scope.configure_stim(
            channel=channel,
            scale_v=scale_v,
            offset_v=offset_v,
            time_scale_s=time_scale_s,
            time_offset_s=time_offset_s,
            trigger_level_v=trigger_level_v,
            trigger_sweep=trigger_sweep,
        )
        t, v, callback_result = scope.capture_during_pattern_run(
            run_fn,
            channel=channel,
            points=points,
            timeout_s=timeout_s,
            arm="run",
            run_join_timeout_s=run_join_timeout_s,
            waveform_mode="NORM",
            score_expected_v=expected_plateau_v,
        )
    summary: dict[str, Any] = {
        "ok": True,
        "resource": res,
        "channel": channel,
        "callback_result": callback_result,
        **_pulse_summary(t, v, expected_plateau_v or threshold_v),
    }
    if save_csv:
        path = Path(save_csv).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savetxt(path, np.column_stack([t, v]), delimiter=",", header="time_s,voltage_v", comments="")
        summary["csv"] = str(path)
    if return_waveform:
        summary["time_s"] = t
        summary["volts_v"] = v
    return summary


def configure_scope(
    resource: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        return scope.configure(**kwargs)


def run_scope(resource: str | None = None) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        scope.run()
        return {"ok": True, "resource": res, "state": "RUN"}


def stop_scope(resource: str | None = None) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        scope.stop()
        return {"ok": True, "resource": res, "state": "STOP"}


def scope_status(resource: str | None = None) -> dict[str, Any]:
    res = find_rigol_resource(resource)
    if not res:
        return {"ok": False, "error": "Rigol не найден"}
    with open_instrument(res) as scope:
        return {"ok": True, **scope.status()}


INTAN_STIM_WIRING = {
    "CH1": "Stim load (10 kΩ → GND), ожидаем ~1.8 V при 180 µA",
    "trigger": "Rising на CH1, Normal/Auto",
    "coupling": "DC, 1 MΩ",
    "scale": "200 mV/div или 500 mV/div для импульсов µs..ms",
}
