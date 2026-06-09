"""RHS1 USB HS streaming frames (STM32_SPI_CO USB V2)."""

from __future__ import annotations

import array
import struct
from dataclasses import dataclass

from usb_intan_lib import FRAME_MAGIC, FRAME_SIZE

RHS1_HEADER = struct.Struct("<IHHIIIIII")
RHS1_RESPONSES_MAX = 2032
RHS1_TAGGED_SAMPLES_MAX = 1016
RR8_CHANNELS = 8
RHS1_FLAG_COUNTER = 0x0001
RHS1_FLAG_REAL_ADC = 0x0002
RHS1_FLAG_RR = 0x0004
RHS1_FLAG_CHANNEL_TAG = 0x0008


def _meta_first_channel(meta: int) -> int:
    return meta & 0xFF


def _meta_channel_count(meta: int) -> int:
    return (meta >> 8) & 0xFF


def _meta_convert_flags(meta: int) -> int:
    return (meta >> 16) & 0xFF


def _meta_channel_bits(meta: int) -> int:
    return (meta >> 24) & 0x7


@dataclass(frozen=True)
class Rhs1TaggedSample:
    channel: int
    adc: int


@dataclass(frozen=True)
class Rhs1FrameMeta:
    """Заголовок RHS1 без разбора payload (быстрый путь USB→UDP)."""

    flags: int
    frame_seq: int
    first_sample_counter: int
    sample_count: int
    spi_overflow_count: int
    usb_overflow_count: int
    reserved: int
    first_channel: int
    channel_count: int
    convert_flags: int
    channel_bits: int
    channel_tagged: bool


@dataclass(frozen=True)
class Rhs1Frame:
    flags: int
    frame_seq: int
    first_sample_counter: int
    sample_count: int
    spi_overflow_count: int
    usb_overflow_count: int
    reserved: int
    first_channel: int
    channel_count: int
    convert_flags: int
    channel_bits: int
    channel_tagged: bool
    responses: tuple[int, ...]
    tagged_samples: tuple[Rhs1TaggedSample, ...]


def _parse_rhs1_header_fields(payload: bytes) -> Rhs1FrameMeta:
    if len(payload) != FRAME_SIZE:
        raise ValueError(f"RHS1 frame length {len(payload)} != {FRAME_SIZE}")

    (
        magic,
        version,
        flags,
        frame_seq,
        first_sc,
        sample_count,
        spi_ovf,
        usb_ovf,
        reserved,
    ) = RHS1_HEADER.unpack_from(payload, 0)

    if magic != FRAME_MAGIC:
        raise ValueError(f"bad RHS1 magic 0x{magic:08X}")
    if version != 1:
        raise ValueError(f"bad RHS1 version {version}")
    if sample_count <= 0:
        raise ValueError(f"bad sample_count {sample_count}")

    channel_tagged = bool(flags & RHS1_FLAG_CHANNEL_TAG)
    max_samples = RHS1_TAGGED_SAMPLES_MAX if channel_tagged else RHS1_RESPONSES_MAX
    if sample_count > max_samples:
        raise ValueError(
            f"bad sample_count {sample_count} (max {max_samples}, tagged={channel_tagged})"
        )

    channel_count = _meta_channel_count(reserved)
    if channel_count <= 0:
        channel_count = RR8_CHANNELS if (flags & RHS1_FLAG_RR) else 1
    first_channel = _meta_first_channel(reserved)

    return Rhs1FrameMeta(
        flags=flags,
        frame_seq=frame_seq,
        first_sample_counter=first_sc,
        sample_count=sample_count,
        spi_overflow_count=spi_ovf,
        usb_overflow_count=usb_ovf,
        reserved=reserved,
        first_channel=first_channel,
        channel_count=channel_count,
        convert_flags=_meta_convert_flags(reserved),
        channel_bits=_meta_channel_bits(reserved),
        channel_tagged=channel_tagged,
    )


def parse_rhs1_header(payload: bytes) -> Rhs1FrameMeta:
    return _parse_rhs1_header_fields(payload)


def parse_rhs1_frame(payload: bytes) -> Rhs1Frame:
    meta = _parse_rhs1_header_fields(payload)
    flags = meta.flags
    sample_count = meta.sample_count
    channel_tagged = meta.channel_tagged
    channel_count = meta.channel_count
    first_channel = meta.first_channel

    values_offset = RHS1_HEADER.size
    tagged_samples: tuple[Rhs1TaggedSample, ...] = ()
    responses: tuple[int, ...] = ()

    if channel_tagged:
        raw = struct.unpack_from(f"<{sample_count}I", payload, values_offset)
        tagged_samples = tuple(
            Rhs1TaggedSample(channel=(word >> 16) & 0xF, adc=word & 0xFFFF)
            for word in raw
        )
    else:
        raw = struct.unpack_from(f"<{sample_count}H", payload, values_offset)
        responses = tuple(raw)

    return Rhs1Frame(
        flags=flags,
        frame_seq=meta.frame_seq,
        first_sample_counter=meta.first_sample_counter,
        sample_count=sample_count,
        spi_overflow_count=meta.spi_overflow_count,
        usb_overflow_count=meta.usb_overflow_count,
        reserved=meta.reserved,
        first_channel=first_channel,
        channel_count=channel_count,
        convert_flags=meta.convert_flags,
        channel_bits=meta.channel_bits,
        channel_tagged=channel_tagged,
        responses=responses,
        tagged_samples=tagged_samples,
    )


def _tagged_adc_bytes_from_raw(
    payload: bytes,
    sample_count: int,
    *,
    want_channel: int | None = None,
    channel_set: set[int] | None = None,
) -> bytes:
    off = RHS1_HEADER.size
    words = struct.unpack_from(f"<{sample_count}I", payload, off)
    if want_channel is not None:
        adcs = (word & 0xFFFF for word in words if ((word >> 16) & 0xF) == want_channel)
    elif channel_set is not None:
        adcs = (word & 0xFFFF for word in words if ((word >> 16) & 0xF) in channel_set)
    else:
        adcs = (word & 0xFFFF for word in words)
    return array.array("H", adcs).tobytes()


def _tagged_words_to_interleaved(
    words: tuple[int, ...],
    channels: list[int],
    *,
    validate_tags: bool = False,
    nch_stream: int | None = None,
    first_channel: int = 0,
    first_sample_counter: int = 0,
) -> tuple[bytes, int]:
    """Сборка interleaved uint16 по полю channel в CHANNEL_TAG (не по порядку в потоке)."""
    nch = len(channels)
    if nch <= 0:
        return b"", 0

    channel_set = set(channels)
    pending: dict[int, list[int]] = {ch: [] for ch in channels}
    tag_errors = 0
    rr_span = nch_stream if nch_stream and nch_stream > 0 else nch

    for i, word in enumerate(words):
        ch = (word >> 16) & 0xF
        if validate_tags and ch in channel_set:
            want = first_channel + ((first_sample_counter + i) % rr_span)
            if ch != want:
                tag_errors += 1
        if ch in channel_set:
            pending[ch].append(word & 0xFFFF)

    min_len = min(len(pending[ch]) for ch in channels)
    if min_len <= 0:
        return b"", tag_errors

    adcs: list[int] = []
    for _ in range(min_len):
        for ch in channels:
            adcs.append(pending[ch].pop(0))
    return struct.pack(f"<{len(adcs)}H", *adcs), tag_errors


def tagged_raw_to_interleaved_bytes(
    payload: bytes,
    meta: Rhs1FrameMeta,
    channels: list[int],
    *,
    validate_tags: bool = False,
) -> tuple[bytes, int]:
    """CHANNEL_TAG RHS1 raw → interleaved uint16 без Rhs1TaggedSample."""
    nch = len(channels)
    if nch <= 0:
        return b"", 0

    off = RHS1_HEADER.size
    words = struct.unpack_from(f"<{meta.sample_count}I", payload, off)
    return _tagged_words_to_interleaved(
        words,
        channels,
        validate_tags=validate_tags,
        nch_stream=meta.channel_count,
        first_channel=meta.first_channel,
        first_sample_counter=meta.first_sample_counter,
    )


def rhs1_raw_payload_bytes(
    payload: bytes,
    requested_channels: list[int] | None = None,
    meta: Rhs1FrameMeta | None = None,
) -> bytes:
    """Быстрое извлечение uint16 payload из сырого RHS1-кадра."""
    if meta is None:
        meta = parse_rhs1_header(payload)

    if meta.channel_tagged:
        if requested_channels and len(requested_channels) > 1:
            raise ValueError(
                "tagged RHS1 frame requires Rhs1ChannelRouter for multi-channel recording"
            )
        if requested_channels and len(requested_channels) == 1:
            return _tagged_adc_bytes_from_raw(
                payload, meta.sample_count, want_channel=requested_channels[0]
            )
        return _tagged_adc_bytes_from_raw(payload, meta.sample_count)

    end = RHS1_HEADER.size + meta.sample_count * 2
    return payload[RHS1_HEADER.size : end]


def expected_tagged_channel(frame: Rhs1Frame, sample_index: int) -> int:
    """Канал по глобальному sample counter (как в tools/usb_frame_channel_tag.py)."""
    nch = frame.channel_count if frame.channel_count > 0 else 1
    return frame.first_channel + ((frame.first_sample_counter + sample_index) % nch)


def tagged_frame_to_interleaved_bytes(
    frame: Rhs1Frame,
    channels: list[int],
    *,
    validate_tags: bool = True,
) -> tuple[bytes, int]:
    """CHANNEL_TAG RHS1 → плотный interleaved uint16 для UDP (маршрутизация по тегу)."""
    words = tuple(
        (sample.channel << 16) | (sample.adc & 0xFFFF)
        for sample in frame.tagged_samples
    )
    return _tagged_words_to_interleaved(
        words,
        channels,
        validate_tags=validate_tags,
        nch_stream=frame.channel_count,
        first_channel=frame.first_channel,
        first_sample_counter=frame.first_sample_counter,
    )


def rhs1_frame_to_payload_bytes(
    frame: Rhs1Frame,
    requested_channels: list[int] | None = None,
    *,
    max_samples: int | None = None,
) -> bytes:
    """
    Собирает плотный uint16 payload из одноканального (untagged) RHS1-кадра.

    Для CHANNEL_TAG многоканального потока используйте Rhs1ChannelRouter.
    """
    if frame.channel_tagged:
        if requested_channels and len(requested_channels) > 1:
            raise ValueError(
                "tagged RHS1 frame requires Rhs1ChannelRouter for multi-channel recording"
            )
        limit = max_samples if max_samples is not None else frame.sample_count
        if requested_channels and len(requested_channels) == 1:
            want_ch = requested_channels[0]
            adcs = [
                sample.adc
                for sample in frame.tagged_samples
                if sample.channel == want_ch
            ][:limit]
        else:
            adcs = [sample.adc for sample in frame.tagged_samples[:limit]]
        if not adcs:
            return b""
        return struct.pack(f"<{len(adcs)}H", *adcs)

    responses = frame.responses
    if max_samples is not None:
        responses = responses[:max_samples]
    if not responses:
        return b""
    return struct.pack(f"<{len(responses)}H", *responses)


class Rhs1ChannelRouter:
    """
    Собирает interleaved uint16 из RHS1 CHANNEL_TAG потока.

    Значения маршрутизируются по полю channel в теге, а не по позиции в кадре.
    Это сохраняет выравнивание при RR16 (1016 сэмплов/кадр не кратно 16) и после
    границ USB-чанков.
    """

    def __init__(self, channels: list[int]):
        self.channels = list(channels)
        self.tag_errors = 0

    def feed_frame(self, frame: Rhs1Frame) -> bytes:
        return self.feed_raw_from_frame(frame)

    def feed_raw(
        self,
        payload: bytes,
        meta: Rhs1FrameMeta,
        *,
        validate_tags: bool = False,
    ) -> bytes:
        if not meta.channel_tagged:
            chunk = rhs1_raw_payload_bytes(payload, self.channels, meta)
            return pack_rr8_multichannel(chunk, len(self.channels))

        compact, errors = tagged_raw_to_interleaved_bytes(
            payload, meta, self.channels, validate_tags=validate_tags
        )
        self.tag_errors += errors
        return compact

    def feed_raw_from_frame(self, frame: Rhs1Frame) -> bytes:
        if not frame.channel_tagged:
            payload = rhs1_frame_to_payload_bytes(frame, self.channels)
            return pack_rr8_multichannel(payload, len(self.channels))

        payload, errors = tagged_frame_to_interleaved_bytes(
            frame, self.channels, validate_tags=False
        )
        self.tag_errors += errors
        return payload

    def flush(self) -> bytes:
        return b""


def pack_rr8_multichannel(payload: bytes, channel_count: int) -> bytes:
    """
    RR8 / interleaved uint16 stream → плотные кадры channel_count×uint16 LE.
    Обрезает хвост, если число сэмплов не кратно channel_count.
    """
    if channel_count <= 0:
        return b""
    if len(payload) % 2:
        payload = payload[: len(payload) - 1]
    total_samples = len(payload) // 2
    complete_frames = total_samples // channel_count
    if complete_frames <= 0:
        return b""
    use_bytes = complete_frames * channel_count * 2
    return payload[:use_bytes]


def mux63_extract(payload: bytes, channels: list[int]) -> bytes:
    """Выборка каналов из mux63-потока (16 значений на «кадр»)."""
    if not channels:
        return b""
    n = len(payload) // 2
    if n == 0:
        return b""
    out = bytearray()
    for i in range(0, n, MUX_FRAME_CHANNELS):
        frame_end = min(i + MUX_FRAME_CHANNELS, n)
        if frame_end - i < MUX_FRAME_CHANNELS:
            break
        values = struct.unpack_from(f"<{MUX_FRAME_CHANNELS}H", payload, i * 2)
        for ch in channels:
            if 0 <= ch < MUX_FRAME_CHANNELS:
                out.extend(struct.pack("<H", values[ch]))
    return bytes(out)


MUX_FRAME_CHANNELS = 16
