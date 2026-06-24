"""Must match INTAN_FW_CONVERT_SLOTS / INTAN_FW_KSPS_DEFAULT in Core/Inc/intan_fw_acq.h."""

N_CH = 8

# Единственный валидированный production rate (RR8, clip=0, 10 s soak).
FW_KSPS_DEFAULT = 40


def fw_stream_cmd(n_per_ch: int, ksps: int = FW_KSPS_DEFAULT, ch: int = 255) -> str:
    return f"SPI_STREAM_FW {n_per_ch} {ch} 0 {ksps}"
