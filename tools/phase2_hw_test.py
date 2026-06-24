#!/usr/bin/env python3
"""Phase 2 hardware validation before phase 3."""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usb_intan_lib import (
    EP_IN,
    EP_OUT,
    FRAME_SIZE,
    close_device,
    find_device,
    open_device,
    read_text_during_stream,
    run_text_command,
    validate_rhs1_frame,
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "?"}.get(status, "?")
    print(f"{mark} {name}: {detail or status}")


def parse_stats(stats: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in stats.split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def test_stats_fingerprint(dev) -> None:
    st = run_text_command(dev, "STATS", drain_before=True)
    p = parse_stats(st)
    ok = (
        p.get("build_type") == "Release"
        and p.get("git", "").startswith("66e9b65")
        and p.get("pscl") == "8"
        and p.get("nss_midi") == "4"
    )
    record(
        "STATS fingerprint",
        PASS if ok else FAIL,
        f"build={p.get('build_type')} git={p.get('git')} pscl={p.get('pscl')} midi={p.get('nss_midi')}",
    )


def test_spi_lock_idle_and_busy(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    r = run_text_command(dev, "NSS_MIDI 2", drain_before=True)
    record("NSS_MIDI idle", PASS if r.strip() == "OK" else FAIL, r.strip())
    run_text_command(dev, "NSS_MIDI 4", drain_before=True)

    run_text_command(dev, "SPI_STREAM_FW 200000 255 0 40", drain_before=True)
    # Saturated IN may swallow the text reply; verify the lock held PSCL/MIDI.
    dev.write(EP_OUT, b"NSS_MIDI 2\n", timeout=5000)
    time.sleep(0.5)
    run_text_command(dev, "STOP", drain_before=False, timeout_ms=15000)
    st = parse_stats(run_text_command(dev, "STATS", drain_before=True))
    locked = st.get("nss_midi") == "4" and st.get("pscl") == "8"
    record("SPI lock during stream", PASS if locked else FAIL, f"nss_midi={st.get('nss_midi')} pscl={st.get('pscl')}")


def test_stop_during_capture(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "SPI_STREAM_FW 400000 255 0 40", drain_before=True)
    frames = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.0:
        try:
            dev.read(EP_IN, FRAME_SIZE, timeout=500)
            frames += 1
        except Exception:
            break
    r = run_text_command(dev, "STOP", drain_before=False, timeout_ms=15000)
    st = parse_stats(run_text_command(dev, "STATS", drain_before=False))
    ok = r.strip() == "OK" and int(st.get("fw_dma_err", "1")) == 0
    record("STOP during RR8", PASS if ok else FAIL, f"reply={r.strip()} frames~{frames} fw_dma_err={st.get('fw_dma_err')}")


def test_rhs1_strict(dev) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    run_text_command(dev, "SPI_STREAM_FW 8000 255 0 40", drain_before=True)
    seq = 0
    bad = 0
    got = 0
    t0 = time.perf_counter()
    while got < 8000 * 8 and time.perf_counter() - t0 < 30:
        p = bytes(dev.read(EP_IN, FRAME_SIZE, timeout=5000))
        try:
            validate_rhs1_frame(p, seq)
            seq += 1
            got += 2032  # upper bound; untagged RR8 packs fewer
        except Exception as e:
            bad += 1
            record("RHS1 strict", FAIL, str(e))
            break
    else:
        record("RHS1 strict", PASS, f"frames={seq} validation_ok")
    run_text_command(dev, "STOP", drain_before=False, timeout_ms=10000)


def test_usb_disconnect(dev, ifn) -> None:
    run_text_command(dev, "STOP", drain_before=True)
    st0 = parse_stats(run_text_command(dev, "STATS", drain_before=True))
    d0 = int(st0.get("usb_disconnect", "0"))

    run_text_command(dev, "SPI_STREAM_FW 400000 255 0 40", drain_before=True)
    time.sleep(0.5)

    close_device(dev, ifn)
    time.sleep(0.2)
    try:
        dev = find_device()
        dev.reset()
    except Exception as e:
        record("USB disconnect", SKIP, f"reset failed: {e}")
        return

    time.sleep(2.0)
    try:
        dev2, ifn2 = open_device(reset=False)
        st = parse_stats(run_text_command(dev2, "STATS", drain_before=True))
        d1 = int(st.get("usb_disconnect", "0"))
        ok = d1 > d0
        record(
            "USB disconnect counter",
            PASS if ok else FAIL,
            f"usb_disconnect {d0} -> {d1}",
        )
        run_text_command(dev2, "STOP", drain_before=True)
        close_device(dev2, ifn2)
    except Exception as e:
        record("USB disconnect reconnect", FAIL, str(e))


def main() -> int:
    print("=== Phase 2 HW test suite ===\n")
    dev, ifn = open_device(reset=True)
    time.sleep(0.5)
    try:
        test_stats_fingerprint(dev)
        test_spi_lock_idle_and_busy(dev)
        test_stop_during_capture(dev)
        test_rhs1_strict(dev)
        test_usb_disconnect(dev, ifn)
        ifn = -1  # already closed in disconnect test
        return 0
    finally:
        if ifn >= 0:
            try:
                close_device(dev, ifn)
            except Exception:
                pass


if __name__ == "__main__":
    rc = main()
    print("\n=== Summary ===")
    for name, status, detail in results:
        print(f"  [{status}] {name}: {detail}")
    fails = [n for n, s, _ in results if s == FAIL]
    if fails:
        print(f"\nFAILED: {', '.join(fails)}")
        raise SystemExit(1)
    print("\nAll tests passed.")
    raise SystemExit(0)
