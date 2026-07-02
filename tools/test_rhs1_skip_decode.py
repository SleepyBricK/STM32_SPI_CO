#!/usr/bin/env python3
"""Simulate dropped RHS1 frames and verify untagged RR8 channel phase."""

from __future__ import annotations

import argparse
import statistics
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usb_intan_lib import FRAME_MAGIC, FRAME_SIZE, FRAME_VERSION, Rhs1FwDecodeState, iter_rhs1_fw_samples

HEADER = struct.Struct("<IHHIIIIII")
N_CH = 8
CH_ADC = [0x7FFE, 0x9001, 0xA002, 0xB003, 0xC004, 0xD005, 0xE006, 0xF007]


def make_frame(seq: int, first_sc: int, sample_count: int) -> bytes:
    frame = bytearray(FRAME_SIZE)
    HEADER.pack_into(
        frame,
        0,
        FRAME_MAGIC,
        FRAME_VERSION,
        0x0002,
        seq,
        first_sc,
        sample_count,
        0,
        0,
        0,
    )
    for i in range(sample_count):
        ch = (first_sc + i) % N_CH
        struct.pack_into("<H", frame, 32 + i * 2, CH_ADC[ch])
    return bytes(frame)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=64)
    ap.add_argument("--skip-every", type=int, default=5)
    ap.add_argument("--sample-count", type=int, default=2032)
    ap.add_argument(
        "--phase-step",
        type=int,
        default=2033,
        help="first_sample_counter increment per frame; non-multiple of 8 exposes host-index drift",
    )
    args = ap.parse_args()

    state = Rhs1FwDecodeState(strict_seq=False)
    ch0: list[int] = []
    skipped = 0
    for seq in range(args.frames):
        if args.skip_every > 0 and seq > 0 and seq % args.skip_every == 0:
            skipped += 1
            continue
        frame = make_frame(seq, seq * args.phase_step, args.sample_count)
        for ch, adc in iter_rhs1_fw_samples(frame, state, n_ch=N_CH):
            if ch == 0:
                ch0.append(adc)

    med = int(statistics.median(ch0)) if ch0 else -1
    if med != CH_ADC[0]:
        print(f"FAIL: ch0 median=0x{med:04X}, expected 0x{CH_ADC[0]:04X}, skipped={skipped}", file=sys.stderr)
        return 1
    print(
        f"PASS: ch0 median=0x{med:04X}, samples={len(ch0)}, skipped_frames={skipped}, seq_gaps={state.seq_gaps}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
