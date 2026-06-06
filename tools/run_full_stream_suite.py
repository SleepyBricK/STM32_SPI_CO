#!/usr/bin/env python3
"""Полный прогон stream: single, RR8, кастомный range, графики, PASS/FAIL."""

from __future__ import annotations

import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_intan_lib import EP_IN, FRAME_SIZE, close_device, open_device, run_text_command

HDR = struct.Struct("<IHHIIIIII")
UV = 0.195
MID = 32768.0
SKIP = 500
TOOLS = Path(__file__).resolve().parent


@dataclass
class ChMetrics:
    n: int = 0
    rms: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    ptp: float = 0.0
    gt500: int = 0
    gt1m: int = 0
    raw0: int = 0
    spikes: int = 0
    quiet80: float = 0.0
    uv: np.ndarray = field(default_factory=lambda: np.array([]))


def metrics_from_codes(codes: list[int] | np.ndarray) -> ChMetrics:
    c = np.asarray(codes, dtype=np.uint16)
    uv = (c.astype(np.float64) - MID) * UV
    u = uv[SKIP:] if len(uv) > SKIP else uv
    if len(u) == 0:
        return ChMetrics(n=len(c), uv=uv)
    return ChMetrics(
        n=len(c),
        rms=float(np.sqrt(np.mean(u * u))),
        mean=float(np.mean(u)),
        std=float(np.std(u)),
        ptp=float(np.max(u) - np.min(u)),
        gt500=int(np.sum(np.abs(u) > 500)),
        gt1m=int(np.sum(np.abs(u) > 1000)),
        raw0=int(np.sum(c == 0)),
        spikes=int(np.sum((c >= 0xC000) & (c < 0xD000))),
        quiet80=100.0 * float(np.sum(np.abs(u) < 80)) / len(u),
        uv=uv,
    )


def read_single(dev, ch: int, n: int) -> tuple[list[int], float, str]:
    reply = run_text_command(dev, f"SPI_STREAM_REAL {n} {ch} 0", timeout_ms=600000, drain_before=True)
    if not reply.strip().startswith("OK"):
        raise RuntimeError(f"SPI_STREAM_REAL ch{ch}: {reply}")
    codes: list[int] = []
    t0 = time.perf_counter()
    while len(codes) < n:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=600000))
        _, _, _, _, _, sc, _, _, _ = HDR.unpack_from(pkt, 0)
        for i in range(sc):
            if len(codes) >= n:
                break
            codes.append(struct.unpack_from("<H", pkt, 32 + i * 2)[0])
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    return codes, elapsed, stats


def read_tagged(dev, cmd: str, total: int, first: int, count: int) -> tuple[list[list[int]], float, str, int]:
    reply = run_text_command(dev, cmd, timeout_ms=600000, drain_before=True)
    if not reply.strip().startswith("OK"):
        raise RuntimeError(f"{cmd}: {reply}")
    buckets: list[list[int]] = [[] for _ in range(count)]
    tag_errors = 0
    idx = 0
    seq = 0
    t0 = time.perf_counter()
    while idx < total:
        pkt = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=600000))
        _, _, flags, frame_seq, _, sc, spi_ovf, usb_ovf, meta = HDR.unpack_from(pkt, 0)
        if frame_seq != seq:
            pass
        if spi_ovf or usb_ovf:
            raise RuntimeError(f"overflow frame={seq} spi={spi_ovf} usb={usb_ovf}")
        fc = meta & 0xFF
        cc = (meta >> 8) & 0xFF
        if fc != first or cc != count:
            raise RuntimeError(f"meta first={fc} count={cc} want {first}/{count}")
        for i in range(sc):
            if idx >= total:
                break
            word = struct.unpack_from("<I", pkt, 32 + i * 4)[0]
            adc = word & 0xFFFF
            ch = (word >> 16) & 0xF
            want = first + (idx % count)
            if ch != want:
                tag_errors += 1
            if first <= ch < first + count:
                buckets[ch - first].append(adc)
            idx += 1
        seq += 1
    elapsed = time.perf_counter() - t0
    stats = run_text_command(dev, "STATS", timeout_ms=10000, drain_before=False).strip()
    run_text_command(dev, "STOP", timeout_ms=5000, drain_before=False)
    return buckets, elapsed, stats, tag_errors


def corr(a: np.ndarray, b: np.ndarray, n: int = 20000) -> float:
    n = min(n, len(a) - SKIP, len(b) - SKIP)
    if n < 100:
        return float("nan")
    x = a[SKIP : SKIP + n]
    y = b[SKIP : SKIP + n]
    return float(np.corrcoef(x, y)[0, 1])


def plot_range_3s(buckets: list[list[int]], first: int, elapsed: float, out: Path, title: str) -> None:
    count = len(buckets)
    rows = (count + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(14, 3 * rows), layout="constrained")
    axes_flat = np.atleast_1d(axes).flat
    for i in range(count):
        ch = first + i
        m = metrics_from_codes(buckets[i])
        fs = len(buckets[i]) / elapsed if elapsed > 0 else 1.0
        show_s = min(3.0, len(m.uv) / fs) if fs > 0 else 3.0
        n = min(len(m.uv), int(show_s * fs))
        t = np.arange(n) / fs
        ax = axes_flat[i]
        ax.plot(t, m.uv[:n], lw=0.3)
        ax.set_title(f"ch{ch}  RMS={m.rms:.0f} µV  quiet±80={m.quiet80:.0f}%")
        ax.set_xlabel("с")
        ax.set_ylabel("µV")
        ax.grid(True, alpha=0.3)
    for j in range(count, len(axes_flat)):
        axes_flat[j].set_visible(False)
    fig.suptitle(title, fontsize=12)
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main() -> int:
    fails: list[str] = []
    lines: list[str] = ["=== Full stream suite ===", f"time={time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    dur_s = 3.0
    n_single = int(dur_s * 420000)
    total_rr8 = 825000
    n_per_range = int(dur_s * 350000 / 3)  # ~3 ch, ~350 kS/s agg
    total_range13 = n_per_range * 3
    total_range24 = int(dur_s * 350000 / 4) * 4

    dev, ifn = open_device(reset=True)
    time.sleep(0.5)
    lines.append(run_text_command(dev, "ID", timeout_ms=10000, drain_before=True).strip())
    lines.append(run_text_command(dev, "STATS", timeout_ms=5000, drain_before=True).strip())
    lines.append("")

    # --- single ch2 GND ---
    print("1/6 single ch2 GND...")
    c2, el2, st2 = read_single(dev, 2, n_single)
    m2s = metrics_from_codes(c2)
    fs2 = len(c2) / el2
    lines += [
        f"SINGLE ch2 (10k GND): n={m2s.n} elapsed={el2:.2f}s fs={fs2/1000:.1f} kS/s",
        f"  rms={m2s.rms:.1f} uV quiet80={m2s.quiet80:.1f}% gt500={m2s.gt500} raw0={m2s.raw0} spikes={m2s.spikes}",
        f"  {st2}",
        "",
    ]
    if m2s.rms > 80 or m2s.raw0 or m2s.gt500 > 500:
        fails.append(f"single ch2 rms={m2s.rms:.0f} gt500={m2s.gt500}")
    if fs2 / 1000 < 330:
        fails.append(f"single ch2 fs={fs2/1000:.0f} kS/s")
    if "usb_ovf=0" not in st2:
        fails.append("single ch2 usb_ovf")

    time.sleep(0.3)

    # --- single ch0 float ---
    print("2/6 single ch0 float...")
    c0, el0, st0 = read_single(dev, 0, n_single)
    m0s = metrics_from_codes(c0)
    fs0 = len(c0) / el0
    lines += [
        f"SINGLE ch0 (float): n={m0s.n} elapsed={el0:.2f}s fs={fs0/1000:.1f} kS/s",
        f"  rms={m0s.rms:.1f} uV gt500={m0s.gt500} raw0={m0s.raw0}",
        "",
    ]
    if m0s.rms < 200:
        fails.append(f"single ch0 too quiet rms={m0s.rms:.0f}")
    c02s = corr(m0s.uv, m2s.uv)
    if abs(c02s) > 0.5:
        fails.append(f"single ch0-ch2 corr={c02s:.2f}")

    time.sleep(0.3)

    # --- RR8 3s ---
    print("3/6 RR8 3s...")
    br8, elr, strr, tags_r8 = read_tagged(dev, f"SPI_STREAM_RR8_REAL {total_rr8} 0", total_rr8, 0, 8)
    agg_r8 = total_rr8 / elr / 1000
    mr8 = [metrics_from_codes(b) for b in br8]
    lines += [
        f"RR8: elapsed={elr:.2f}s agg={agg_r8:.1f} kS/s per_ch={len(br8[0])/elr/1000:.1f} kS/s tag_errors={tags_r8}",
        f"  {strr}",
    ]
    for ch in range(8):
        lines.append(
            f"  ch{ch}: rms={mr8[ch].rms:.0f} quiet80={mr8[ch].quiet80:.0f}% gt500={mr8[ch].gt500} raw0={mr8[ch].raw0}"
        )
    lines.append("")
    if tags_r8:
        fails.append(f"RR8 tags={tags_r8}")
    if "usb_ovf=0" not in strr:
        fails.append("RR8 usb_ovf")
    if agg_r8 < 250:
        fails.append(f"RR8 agg={agg_r8:.0f} kS/s")
    if mr8[2].rms > 100:
        fails.append(f"RR8 ch2 rms={mr8[2].rms:.0f}")
    if mr8[2].raw0:
        fails.append("RR8 ch2 raw0")
    if abs(corr(mr8[0].uv, mr8[2].uv)) > 0.5:
        fails.append(f"RR8 ch0-ch2 corr={corr(mr8[0].uv, mr8[2].uv):.2f}")
    if mr8[0].rms < 100:
        fails.append("RR8 ch0 too quiet")
    # ch2 RR8 vs single
    c02r = corr(m2s.uv, mr8[2].uv)
    lines.append(f"  corr single-ch2 vs RR8-ch2: {c02r:+.3f}")
    lines.append("")

    out_rr8 = TOOLS / "ch0_7_rr8_3s_suite.png"
    plot_range_3s(br8, 0, elr, out_rr8, f"RR8 {elr:.1f}s agg={agg_r8:.0f} kS/s per_ch={len(br8[0])/elr/1000:.0f} kS/s")

    time.sleep(0.3)

    # --- RANGE 1..3 ---
    print("4/6 RANGE ch1-3 3s...")
    first, count = 1, 3
    bg13, elg, stg, tags_g = read_tagged(
        dev,
        f"SPI_STREAM_RANGE_REAL {total_range13} {first} {count} 0",
        total_range13,
        first,
        count,
    )
    mg = [metrics_from_codes(b) for b in bg13]
    agg_g = total_range13 / elg / 1000
    lines += [
        f"RANGE first=1 count=3: elapsed={elg:.2f}s agg={agg_g:.1f} kS/s tag_errors={tags_g}",
        f"  {stg}",
    ]
    for i, m in enumerate(mg):
        ch = first + i
        lines.append(f"  ch{ch}: rms={m.rms:.0f} quiet80={m.quiet80:.0f}% gt500={m.gt500} raw0={m.raw0}")
    lines.append("")
    if tags_g:
        fails.append(f"RANGE1-3 tags={tags_g}")
    if mg[1].raw0:
        fails.append("RANGE ch2 raw0")
    if mg[1].rms > 400:
        fails.append(f"RANGE ch2 rms={mg[1].rms:.0f}")
    range_ch2_warn = mg[1].gt500 > 200
    if abs(corr(mg[0].uv, mg[1].uv)) > 0.95:
        fails.append("RANGE ch1-ch2 cloned")

    out_r13 = TOOLS / "range_1_3_3s_suite.png"
    plot_range_3s(bg13, first, elg, out_r13, f"RANGE ch1-3 {elg:.1f}s agg={agg_g:.0f} kS/s")

    time.sleep(0.3)

    # --- RANGE 2..5 ---
    print("5/6 RANGE ch2-5 3s...")
    first, count = 2, 4
    bg24, el2r, st2r, tags24 = read_tagged(
        dev,
        f"SPI_STREAM_RANGE_REAL {total_range24} {first} {count} 0",
        total_range24,
        first,
        count,
    )
    m24 = [metrics_from_codes(b) for b in bg24]
    agg24 = total_range24 / el2r / 1000
    lines += [
        f"RANGE first=2 count=4: elapsed={el2r:.2f}s agg={agg24:.1f} kS/s tag_errors={tags24}",
        f"  {st2r}",
    ]
    for i, m in enumerate(m24):
        ch = first + i
        lines.append(f"  ch{ch}: rms={m.rms:.0f} quiet80={m.quiet80:.0f}% gt500={m.gt500} raw0={m.raw0}")
    lines.append("")
    if tags24:
        fails.append(f"RANGE2-5 tags={tags24}")
    if m24[0].raw0:
        fails.append("RANGE2-5 ch2 raw0")
    if m24[0].rms > 200:
        fails.append(f"RANGE2-5 ch2 rms={m24[0].rms:.0f}")
    range24_ch2_warn = m24[0].gt500 > 200

    out_r24 = TOOLS / "range_2_5_3s_suite.png"
    plot_range_3s(bg24, first, el2r, out_r24, f"RANGE ch2-5 {el2r:.1f}s agg={agg24:.0f} kS/s")

    # ch2 overlay: single vs RR8 vs RANGE1-3
    print("6/6 plots ch2 compare...")
    fig, ax = plt.subplots(1, 1, figsize=(12, 4), layout="constrained")
    t2 = np.arange(len(m2s.uv)) / fs2
    tr = np.arange(len(mr8[2].uv)) / (len(br8[2]) / elr)
    tg = np.arange(len(mg[1].uv)) / (len(bg13[1]) / elg)
    ax.plot(t2, m2s.uv, lw=0.25, label=f"single RMS={m2s.rms:.0f} µV", alpha=0.85)
    ax.plot(tr, mr8[2].uv, lw=0.25, label=f"RR8 RMS={mr8[2].rms:.0f} µV", alpha=0.85)
    ax.plot(tg, mg[1].uv, lw=0.25, label=f"RANGE1-3 RMS={mg[1].rms:.0f} µV", alpha=0.85)
    ax.set_xlim(0, dur_s)
    ax.set_xlabel("с")
    ax.set_ylabel("µV")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    ax.set_title("ch2 (10k GND): single vs RR8 vs RANGE ch1-3")
    out_ch2 = TOOLS / "ch2_single_rr8_range_3s.png"
    fig.savefig(out_ch2, dpi=150)
    plt.close(fig)

    close_device(dev, ifn)

    warnings: list[str] = []
    if range_ch2_warn:
        warnings.append(f"RANGE1-3 ch2 gt500={mg[1].gt500} (noisy neighbors / higher per-ch rate)")
    if range24_ch2_warn:
        warnings.append(f"RANGE2-5 ch2 gt500={m24[0].gt500}")

    verdict = "PASS" if not fails else "FAIL: " + "; ".join(fails)
    lines += [
        f"Plots: {out_rr8.name}, {out_r13.name}, {out_r24.name}, {out_ch2.name}",
        "",
    ]
    if warnings:
        lines.append("WARNINGS:")
        lines.extend(f"  - {w}" for w in warnings)
        lines.append("")
    lines.append(f"VERDICT: {verdict}")
    report = TOOLS / "full_stream_suite_report.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
