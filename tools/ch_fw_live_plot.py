#!/usr/bin/env python3
"""Live RR8 viewer: SPI_STREAM_FW → pyqtgraph (rolling window).

Production path: `SPI_STREAM_FW n 255 0 40`. Stream runs until STOP (window close / q).

Dependencies (host):
    pip install pyqtgraph PyQt5

Example:
    python3 tools/ch_fw_live_plot.py
    python3 tools/ch_fw_live_plot.py --channels 2 --window-s 2 --ylim-uv 100
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fw_constants import FW_KSPS_DEFAULT, N_CH, fw_stream_cmd
from usb_intan_lib import (
    EP_IN,
    FRAME_SIZE,
    Rhs1FwDecodeState,
    close_device,
    iter_rhs1_fw_samples,
    open_device,
    read_text_during_stream,
    run_text_command,
)

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
except ImportError as exc:
    raise SystemExit(
        "pyqtgraph/PyQt5 not installed. Run: pip install pyqtgraph PyQt5"
    ) from exc

UV_PER_CODE = 0.195
ADC_MID = 32768.0
STREAM_SAMPLES_INDEFINITE = 4_294_967_295
MAX_PLOT_POINTS = 12_000
_QtSignal = getattr(QtCore, "pyqtSignal", None) or QtCore.Signal


def stop_live_stream(dev, timeout_ms: int = 10000) -> str:
    """STOP while RHS1 frames are on IN — never drain_before (would hang)."""
    return read_text_during_stream(dev, "STOP", timeout_ms=timeout_ms).strip()


def prep(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "INIT_RECORD 350000", drain_before=True)
    run_text_command(dev, "CLEAR_ADC", timeout_ms=30000, drain_before=True)


class ChannelRing:
    """Fixed-size ring buffer of µV samples (one channel)."""

    __slots__ = ("cap", "buf", "n", "pos", "lock")

    def __init__(self, capacity: int) -> None:
        self.cap = max(1, capacity)
        self.buf = np.empty(self.cap, dtype=np.float32)
        self.n = 0
        self.pos = 0
        self.lock = QtCore.QMutex()

    def extend_uv(self, values: np.ndarray) -> None:
        if values.size == 0:
            return
        vals = values.astype(np.float32, copy=False)
        locker = QtCore.QMutexLocker(self.lock)
        m = int(vals.size)
        if m >= self.cap:
            self.buf[:] = vals[-self.cap :]
            self.n = self.cap
            self.pos = 0
            return

        first = self.cap - self.pos
        if m <= first:
            self.buf[self.pos : self.pos + m] = vals
            self.pos = (self.pos + m) % self.cap
        else:
            self.buf[self.pos :] = vals[:first]
            self.buf[: m - first] = vals[first:]
            self.pos = (self.pos + m) % self.cap

        self.n = min(self.cap, self.n + m)

    def snapshot(self) -> np.ndarray:
        locker = QtCore.QMutexLocker(self.lock)
        if self.n == 0:
            return np.empty(0, dtype=np.float32)
        if self.n < self.cap:
            return self.buf[: self.n].copy()
        start = self.pos
        if start == 0:
            return self.buf.copy()
        return np.concatenate((self.buf[start:], self.buf[:start]))


def parse_channels(text: str | None) -> list[int]:
    if not text:
        return list(range(N_CH))
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        ch = int(part, 10)
        if ch < 0 or ch >= N_CH:
            raise ValueError(f"channel out of range: {ch}")
        if ch not in out:
            out.append(ch)
    if not out:
        raise ValueError("empty --channels")
    return out


def adc_batch_to_uv(adc: np.ndarray) -> np.ndarray:
    return ((adc.astype(np.float64) - ADC_MID) * UV_PER_CODE).astype(np.float32)


def stats_value(line: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}=(\d+)", line)
    return int(match.group(1)) if match else None


def usb_reader_loop(
    dev,
    rings: dict[int, ChannelRing],
    *,
    ksps: int,
    warmup_s: float,
    stats_interval_s: float,
    stop: threading.Event,
    on_stats: callable | None = None,
    on_error: callable | None = None,
) -> tuple[int, int]:
    """Read RHS1 frames until stop is set. Returns (frames, samples)."""
    t0 = time.perf_counter()
    per_ch: dict[int, list[int]] = {ch: [] for ch in rings}
    batch_limit = max(256, int(ksps * 4))
    decode_state = Rhs1FwDecodeState(strict_seq=False)
    frames = 0
    samples = 0
    last_stats = 0.0

    while not stop.is_set():
        try:
            pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=200))
        except Exception as exc:
            if stop.is_set():
                break
            if on_error:
                on_error(f"USB read: {exc}")
            break

        try:
            pairs = list(iter_rhs1_fw_samples(pkt, decode_state, n_ch=N_CH))
        except Exception as exc:
            if on_error:
                on_error(str(exc))
            break
        frames += 1

        warmed = (time.perf_counter() - t0) >= warmup_s
        for ch, adc in pairs:
            samples += 1
            if not warmed:
                continue
            if ch in per_ch:
                per_ch[ch].append(adc)
                if len(per_ch[ch]) >= batch_limit:
                    rings[ch].extend_uv(adc_batch_to_uv(np.asarray(per_ch[ch], dtype=np.uint16)))
                    per_ch[ch].clear()

        if stats_interval_s > 0.0:
            now = time.perf_counter()
            if now - last_stats >= stats_interval_s:
                last_stats = now
                try:
                    line = read_text_during_stream(dev, "STATS", timeout_ms=2000).strip()
                    if line and on_stats:
                        on_stats(line)
                except Exception:
                    pass

    for ch, buf in per_ch.items():
        if buf:
            rings[ch].extend_uv(adc_batch_to_uv(np.asarray(buf, dtype=np.uint16)))
    if decode_state.seq_gaps and on_stats:
        on_stats(f"note: rhs1_seq_resync={decode_state.seq_gaps} (STATS during stream)")
    return frames, samples


class UsbStreamWorker(QtCore.QThread):
    error = _QtSignal(str)
    stats = _QtSignal(str)

    def __init__(
        self,
        dev,
        rings: dict[int, ChannelRing],
        *,
        ksps: int,
        warmup_s: float,
        stats_interval_s: float,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.dev = dev
        self.rings = rings
        self.ksps = ksps
        self.warmup_s = warmup_s
        self.stats_interval_s = stats_interval_s
        self._stop = threading.Event()
        self.frames = 0
        self.samples = 0

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        def on_error(msg: str) -> None:
            self.error.emit(msg)

        self.frames, self.samples = usb_reader_loop(
            self.dev,
            self.rings,
            ksps=self.ksps,
            warmup_s=self.warmup_s,
            stats_interval_s=self.stats_interval_s,
            stop=self._stop,
            on_stats=self.stats.emit,
            on_error=on_error,
        )


class LivePlotWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        dev,
        ifn: int,
        rings: dict[int, ChannelRing],
        worker: UsbStreamWorker,
        *,
        channels: list[int],
        ksps: int,
        window_s: float,
        ylim_uv: float | None,
        refresh_hz: float,
        decimate: int,
        dc_subtract: bool,
    ) -> None:
        super().__init__()
        self.dev = dev
        self.ifn = ifn
        self.rings = rings
        self.worker = worker
        self.channels = channels
        self.ksps = ksps
        self.window_s = window_s
        self.ylim_uv = ylim_uv
        self.decimate = max(1, decimate)
        self.dc_subtract = dc_subtract
        self.fs = ksps * 1000.0
        self.stream_stopped = False
        self.final_stats_done = False
        self.stop_reply = ""
        self.final_stats = ""

        self.setWindowTitle("SPI_STREAM_FW RR8 live")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QGridLayout(central)

        n = len(channels)
        ncols = 2 if n > 1 else 1
        nrows = (n + ncols - 1) // ncols

        self.plots: dict[int, pg.PlotWidget] = {}
        self.curves: dict[int, pg.PlotDataItem] = {}
        for i, ch in enumerate(channels):
            pw = pg.PlotWidget(title=f"ch{ch}")
            pw.showGrid(x=True, y=True, alpha=0.25)
            pw.setLabel("bottom", "с")
            pw.setLabel("left", "µV")
            if ylim_uv is not None:
                pw.setYRange(-ylim_uv, ylim_uv)
            curve = pw.plot(pen=pg.mkPen(width=1))
            self.plots[ch] = pw
            self.curves[ch] = curve
            layout.addWidget(pw, i // ncols, i % ncols)

        self.status = QtWidgets.QLabel("starting…")
        layout.addWidget(self.status, nrows, 0, 1, ncols)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(max(10, int(1000.0 / max(1.0, refresh_hz))))

        worker.error.connect(self._on_error)
        worker.stats.connect(self._on_stats)
        worker.finished.connect(self._on_worker_finished)

        QtGui.QShortcut(QtGui.QKeySequence("Q"), self, self.close)
        QtGui.QShortcut(QtGui.QKeySequence("Escape"), self, self.close)

    def _on_error(self, msg: str) -> None:
        self.status.setText(f"ERROR: {msg}")
        self.status.setStyleSheet("color: #c00;")
        QtCore.QTimer.singleShot(0, self.close)

    def _on_stats(self, line: str) -> None:
        clip = stats_value(line, "sample_clip")
        ovf = stats_value(line, "usb_ovf")
        clip_v = str(clip) if clip is not None else "?"
        ovf_v = str(ovf) if ovf is not None else "?"
        self.status.setText(f"clip={clip_v}  usb_ovf={ovf_v}  |  {line[:120]}")
        if ovf is not None and ovf > 0:
            self.status.setStyleSheet("color: #c60;")

    def _on_worker_finished(self) -> None:
        self.status.setText(self.status.text() + "  |  USB thread stopped")

    def _refresh(self) -> None:
        for ch in self.channels:
            y = self.rings[ch].snapshot()
            if y.size == 0:
                continue
            raw_med_uv = float(np.median(y))
            y_plot = y - raw_med_uv if self.dc_subtract else y
            if self.decimate > 1:
                y_plot = y_plot[:: self.decimate]
            n = y_plot.size
            t = (np.arange(n, dtype=np.float32) - n) / (self.fs / self.decimate)
            self.curves[ch].setData(t, y_plot)
            if self.ylim_uv is None:
                ymax = float(np.max(np.abs(y_plot))) * 1.15
                ymax = max(ymax, 5.0)
                self.plots[ch].setYRange(-ymax, ymax)
            rms = float(np.sqrt(np.mean(y_plot.astype(np.float64) ** 2)))
            med = int(raw_med_uv / UV_PER_CODE + ADC_MID)
            title = f"ch{ch}  med≈0x{med:04X}  RMS={rms:.0f} µV"
            if self.dc_subtract:
                title += "  DC-sub"
            self.plots[ch].setTitle(title)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._timer.stop()
        self.worker.request_stop()
        self.worker.wait(3000)
        if not self.stream_stopped:
            try:
                self.stop_reply = stop_live_stream(self.dev)
                if self.stop_reply:
                    self.status.setText(f"STOP reply: {self.stop_reply}")
                    QtWidgets.QApplication.processEvents()
                self.stream_stopped = True
            except Exception:
                pass
        if not self.final_stats_done:
            try:
                self.final_stats = run_text_command(self.dev, "STATS", timeout_ms=10000, drain_before=False).strip()
                self.final_stats_done = True
                ovf = stats_value(self.final_stats, "usb_ovf")
                if ovf is not None and ovf > 0:
                    self.status.setText(f"WARNING: final STATS usb_ovf={ovf} (USB frames were dropped)")
                    self.status.setStyleSheet("color: #c60;")
                    QtWidgets.QApplication.processEvents()
            except Exception:
                pass
        event.accept()


def channel_report(rings: dict[int, ChannelRing], channels: list[int]) -> list[str]:
    lines: list[str] = []
    for ch in channels:
        y = rings[ch].snapshot()
        if y.size == 0:
            lines.append(f"ch{ch}: no samples")
            continue
        rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
        med = int(np.median(y) / UV_PER_CODE + ADC_MID)
        lines.append(f"ch{ch}: n={y.size} med=0x{med:04X} RMS={rms:.1f} µV")
    return lines


def run_headless(
    dev,
    rings: dict[int, ChannelRing],
    channels: list[int],
    *,
    ksps: int,
    run_s: float,
    warmup_s: float,
    stats_interval_s: float,
) -> tuple[int, int, str, list[str]]:
    stop = threading.Event()
    stats_lines: list[str] = []
    errors: list[str] = []

    def on_stats(line: str) -> None:
        stats_lines.append(line)

    def on_error(msg: str) -> None:
        errors.append(msg)

    thread = threading.Thread(
        target=usb_reader_loop,
        kwargs={
            "dev": dev,
            "rings": rings,
            "ksps": ksps,
            "warmup_s": warmup_s,
            "stats_interval_s": stats_interval_s,
            "stop": stop,
            "on_stats": on_stats,
            "on_error": on_error,
        },
        daemon=True,
    )
    t0 = time.perf_counter()
    thread.start()
    time.sleep(run_s)
    stop.set()
    thread.join(5.0)
    wall = time.perf_counter() - t0
    stop_reply = stop_live_stream(dev)
    report = channel_report(rings, channels)
    if errors:
        raise RuntimeError(errors[0])
    rate = rings[channels[0]].snapshot().size / max(wall - warmup_s, 0.001)
    report.append(f"wall={wall:.2f}s  ~{rate/1000:.1f} kS/s/ch in buffer")
    last_stats = stats_lines[-1] if stats_lines else ""
    if last_stats:
        report.append(last_stats)
    return len(stats_lines), int(rings[channels[0]].snapshot().size), stop_reply, report


def main() -> int:
    ap = argparse.ArgumentParser(description="Live pyqtgraph viewer for SPI_STREAM_FW RR8")
    ap.add_argument("--ksps", type=int, default=FW_KSPS_DEFAULT, help=f"kS/s per channel (default {FW_KSPS_DEFAULT})")
    ap.add_argument(
        "--channels",
        type=str,
        default=None,
        help="comma-separated channels to plot (default: all 0..7)",
    )
    ap.add_argument("--window-s", type=float, default=1.0, help="rolling window length (s)")
    ap.add_argument("--ylim-uv", type=float, default=250.0, help="fixed Y half-range µV; 0 = auto")
    ap.add_argument("--refresh-hz", type=float, default=25.0, help="plot refresh rate")
    ap.add_argument("--decimate", type=int, default=0, help="plot decimation (0 = auto)")
    ap.add_argument(
        "--dc-subtract",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="subtract per-channel median before drawing (GUI default: on)",
    )
    ap.add_argument("--warmup-s", type=float, default=0.5, help="discard first N seconds after stream start")
    ap.add_argument(
        "--stats-interval",
        type=float,
        default=0.0,
        help="unsafe in-stream STATS poll interval; reads EP_IN and hurts throughput (default: 0/off)",
    )
    ap.add_argument(
        "--run-s",
        type=float,
        default=0.0,
        help="headless soak (no GUI); capture N seconds and print report",
    )
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    if args.ksps != FW_KSPS_DEFAULT:
        print(f"warning: production rate is {FW_KSPS_DEFAULT} kS/s/ch; using {args.ksps}", file=sys.stderr)
    if args.stats_interval > 0.0:
        print(
            "warning: --stats-interval polls STATS during stream and may consume RHS1 frames / hurt throughput",
            file=sys.stderr,
        )

    try:
        channels = parse_channels(args.channels)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cap = max(1000, int(args.window_s * args.ksps * 1000))
    ylim_uv = None if args.ylim_uv <= 0.0 else args.ylim_uv

    dev, ifn = open_device(reset=args.reset)
    if args.reset:
        time.sleep(0.3)

    cmd = fw_stream_cmd(STREAM_SAMPLES_INDEFINITE, args.ksps)
    rings = {ch: ChannelRing(cap) for ch in channels}
    ret = 0
    worker: UsbStreamWorker | None = None
    win: LivePlotWindow | None = None
    stream_stopped = False
    final_stats_done = False

    try:
        prep(dev)
        print(f"=== {cmd} ===")
        reply = run_text_command(dev, cmd, timeout_ms=15000, drain_before=True).strip()
        if not reply.startswith("OK"):
            print(f"error: stream start failed: {reply}", file=sys.stderr)
            return 1

        if args.run_s > 0.0:
            print(f"headless capture {args.run_s:.1f}s  channels={channels}")
            _, n_buf, stop_reply, report = run_headless(
                dev,
                rings,
                channels,
                ksps=args.ksps,
                run_s=args.run_s,
                warmup_s=args.warmup_s,
                stats_interval_s=args.stats_interval,
            )
            for line in report:
                print(line)
            print(f"STOP reply: {stop_reply}")
            if not stop_reply.startswith("OK"):
                return 1
            stream_stopped = True
            final_stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
            final_stats_done = True
            print(final_stats)
            clip = stats_value(final_stats, "sample_clip")
            ovf = stats_value(final_stats, "usb_ovf")
            if clip is None:
                print("FAIL: sample_clip missing from STATS", file=sys.stderr)
                return 1
            if ovf is None:
                print("FAIL: usb_ovf missing from STATS", file=sys.stderr)
                return 1
            if clip != 0:
                print("FAIL: sample_clip != 0", file=sys.stderr)
                return 1
            if ovf != 0:
                print("FAIL: usb_ovf != 0", file=sys.stderr)
                return 1
            expected_n = min(cap, max(0.0, args.run_s - args.warmup_s - 0.2) * args.ksps * 1000)
            min_n = int(expected_n * 0.85)
            if n_buf < min_n:
                print(f"FAIL: buffer n={n_buf} < expected ~{min_n}", file=sys.stderr)
                return 1
            print("PASS: live capture OK")
            return 0

        pg.setConfigOptions(antialias=False)
        decimate = args.decimate
        if decimate <= 0:
            decimate = max(1, int(np.ceil(cap / MAX_PLOT_POINTS)))

        worker = UsbStreamWorker(
            dev,
            rings,
            ksps=args.ksps,
            warmup_s=args.warmup_s,
            stats_interval_s=args.stats_interval,
        )
        print("(live until window close / Q)")
        app = QtWidgets.QApplication(sys.argv)
        win = LivePlotWindow(
            dev,
            ifn,
            rings,
            worker,
            channels=channels,
            ksps=args.ksps,
            window_s=args.window_s,
            ylim_uv=ylim_uv,
            refresh_hz=args.refresh_hz,
            decimate=decimate,
            dc_subtract=args.dc_subtract,
        )
        win.resize(1100, 700 if len(channels) <= 2 else 900)
        win.show()
        worker.start()
        ret = int(app.exec())
        if win.stop_reply:
            print(f"STOP reply: {win.stop_reply}")
            stream_stopped = win.stream_stopped
        if win.final_stats:
            print(win.final_stats)
            final_stats_done = win.final_stats_done
        print(f"decimate={decimate}  window={args.window_s}s  channels={channels}")
    finally:
        if worker is not None and worker.isRunning():
            worker.request_stop()
            worker.wait(3000)
        if win is not None:
            stream_stopped = stream_stopped or win.stream_stopped
            final_stats_done = final_stats_done or win.final_stats_done
        if not stream_stopped:
            try:
                stop_reply = stop_live_stream(dev)
                if stop_reply:
                    print(f"STOP reply: {stop_reply}")
                    stream_stopped = True
            except Exception:
                pass
        if not final_stats_done:
            try:
                final_stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
                if final_stats:
                    print(final_stats)
                    ovf = stats_value(final_stats, "usb_ovf")
                    if ovf is not None and ovf > 0:
                        msg = f"WARNING: final STATS usb_ovf={ovf} (USB frames were dropped)"
                        print(msg, file=sys.stderr)
                        if win is not None:
                            win.status.setText(msg)
                            win.status.setStyleSheet("color: #c60;")
                            QtWidgets.QApplication.processEvents()
            except Exception:
                pass
        close_device(dev, ifn)

    return ret


if __name__ == "__main__":
    raise SystemExit(main())
