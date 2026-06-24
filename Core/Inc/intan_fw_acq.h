/**
 * @file intan_fw_acq.h
 * @brief Intan Framework v1.2–style acquisition: TIM → 8×CONVERT (0 AUX) → USB.
 *
 * Mirrors rhsinterface.c sample_processing_routine + userfunctions.c unpack (MISO[slot+2]),
 * with transmit_data_realtime replaced by IntanStream → USB RHS1.
 */

#ifndef INTAN_FW_ACQ_H
#define INTAN_FW_ACQ_H

#include "main.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Validated production rate: SPI_STREAM_FW n 255 0 INTAN_FW_KSPS_DEFAULT (RR8, clip=0). */
#define INTAN_FW_KSPS_DEFAULT       40U

/** ch=255 in SPI_STREAM_FW: all channels (CONVERT 0..7, one sample per ch per sequence). */
#define INTAN_FW_CHANNEL_ALL        255U
#define INTAN_FW_CONVERT_SLOTS      8U
#define INTAN_FW_AUX_SLOTS          0U
#define INTAN_FW_WORDS_PER_SEQ      (INTAN_FW_CONVERT_SLOTS + INTAN_FW_AUX_SLOTS)

/** ksps arg: back-to-back sequences, no TIM6 (max SPI/USB throughput). */
#define INTAN_FW_KSPS_FREERUN       0xFFFF0000U

/** ch=255 at ksps/ch >= this: DWT-paced loop (no TIM6 clip), e.g. 40–70 kS/s RR8. */
#define INTAN_FW_KSPS_DWT_PACE_MIN  15U

/** High-rate SPI tuning disabled — use INTAN_FW_KSPS_DEFAULT only (55+ degrades ADC). */
#define INTAN_FW_KSPS_HIGH_RATE      999U
#define INTAN_FW_KSPS_HIGH_MIDI       2U
#define INTAN_FW_KSPS_FAST_SPI       999U
#define INTAN_FW_KSPS_FAST_PSCL       4U

/** Called from TIM6_DAC_IRQHandler. */
void IntanFw_OnTimerTick(void);

/** Poll from main loop (UsbStreamService_Process). */
void IntanFw_Process(void);

/**
 * Start timer-driven acquisition.
 * @param channel 0..7 single channel, or INTAN_FW_CHANNEL_ALL (255) for all 8.
 * @param n samples per channel (sequences for ALL mode).
 * @param target_ch_ksps 0 selects INTAN_FW_KSPS_DEFAULT; INTAN_FW_KSPS_FREERUN enables max mode.
 */
HAL_StatusTypeDef IntanFw_StreamStart(uint32_t n, uint8_t channel, uint8_t flags, uint32_t target_ch_ksps);

void IntanFw_StreamStop(void);
uint8_t IntanFw_StreamIsActive(void);
uint8_t IntanFw_StreamIsFreerun(void);
/** Tight main-loop pump: FREERUN or DWT-paced RR8 (ksps >= INTAN_FW_KSPS_DWT_PACE_MIN). */
uint8_t IntanFw_StreamUsesHotLoop(void);
uint32_t IntanFw_GetSampleClipCount(void);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_FW_ACQ_H */
