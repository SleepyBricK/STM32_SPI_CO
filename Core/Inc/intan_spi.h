/**
 * @file intan_spi.h
 * @brief Intan RHS2116 — логика SPI как в msu-neuro-terminal-linux (intan_spi.c):
 *        три отдельных CS-фрейма на READ/WRITE/CONVERT, по 32 бита за фрейм (HAL Size=1).
 *
 * Целевая МК: STM32H743VIT6 (как в WeActSTM32H743.ioc, макрос сборки STM32H743xx).
 *
 * Пины (при необходимости поменяйте в этом файле):
 * - CS: PE11 (активный низкий)
 */

#ifndef INTAN_SPI_H
#define INTAN_SPI_H

#include "main.h"
#include "stm32h7xx_hal_spi.h"

#ifdef __cplusplus
extern "C" {
#endif

#define INTAN_CHIP_ID_REG           255U
#define INTAN_RHS2116_CHIP_ID       32U
#define INTAN_SPI_TIMEOUT_MS        100U

/*
 * 0 — Intan не запаян: без SPI2 init/bringup; UART PING/HELP работают.
 * 1 — полный Intan (cmake -DWITH_INTAN_HW=ON или #define INTAN_HW_PRESENT 1).
 */
#ifndef INTAN_HW_PRESENT
#define INTAN_HW_PRESENT  0
#endif

static inline uint8_t Intan_HW_IsPresent(void)
{
  return (INTAN_HW_PRESENT != 0) ? 1U : 0U;
}

#define INTAN_IMPEDANCE_MAX_SAMPLES 128U
#define INTAN_DMA_CHUNK_SLOTS         8192U
/** CONVERT: ADC в RX-слоте k относится к TX-слоту k − LATENCY. */
#define INTAN_CONVERT_PIPELINE_LATENCY 2U

/* Chip Select: выход, idle = high */
#define INTAN_CS_GPIO_PORT   GPIOE
#define INTAN_CS_PIN         GPIO_PIN_11

typedef struct {
  uint8_t channel;
  uint8_t scale_bits;
  uint8_t num_samples;
  uint8_t _pad;
  uint16_t samples[INTAN_IMPEDANCE_MAX_SAMPLES];
} IntanImpedanceArg;

typedef struct {
  uint8_t channel;
  uint8_t scale_bits;
  uint16_t freq_hz;
  uint16_t samples_per_period;
  uint16_t periods;
  uint8_t flags;
} IntanImpedanceTimedArg;

typedef struct {
  int64_t sin_accum;
  int64_t cos_accum;
  int64_t adc_sum;
  uint32_t sample_count;
  uint32_t actual_freq_millihz;
  uint32_t elapsed_cycles;
  uint32_t overruns;
  uint32_t spi_errors;
  uint16_t adc_min;
  uint16_t adc_max;
  uint32_t clipped;
} IntanImpedanceTimedResult;

void Intan_SPI_Init(SPI_HandleTypeDef *hspi);
uint8_t Intan_SPI_IsReady(void);

/** Вызывается из busy-wait SPI/DMA (например при длительном bench). NULL = off. */
typedef void (*Intan_IdleHookFn)(void *ctx);
void Intan_SetIdleHook(Intan_IdleHookFn fn, void *ctx);

/**
 * Минимальная последовательность после power-up (даташит / msu-neuro changelog):
 * CLEAR ADC, R38=0xFFFF (power bug DC), очистка битов «weak» drive в R1 (маска 0x9000).
 * Не заменяет полный INIT_RECORD/INIT_STIM.
 */
HAL_StatusTypeDef Intan_ChipBringup(void);

HAL_StatusTypeDef Intan_ReadReg(uint8_t reg_addr, uint16_t *value);
/** Как Intan_ReadReg, плюс полное 32-бит слово MISO третьей фазы (диагностика). */
HAL_StatusTypeDef Intan_ReadReg_WithRaw(uint8_t reg_addr, uint16_t *value, uint32_t *raw32_out);
HAL_StatusTypeDef Intan_WriteReg(uint8_t reg_addr, uint16_t value, uint8_t u_flag, uint8_t m_flag);
HAL_StatusTypeDef Intan_Convert(uint8_t channel, uint8_t flags, uint16_t *value);
HAL_StatusTypeDef Intan_ConvertPipeline(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *last_value);
HAL_StatusTypeDef Intan_ConvertPipelineRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples);
/** Conservative pipelined polling path: GPIO CS/timing from intan_xfer32(), no DMA/TIM CS. */
HAL_StatusTypeDef Intan_ConvertPipelineSafeRead(uint32_t n, uint8_t channel, uint8_t flags,
                                                uint16_t *samples);
#define INTAN_STREAM_RR8_CHANNELS      8U
#define INTAN_STREAM_RR16_CHANNELS     16U

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples);
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotRead(uint32_t n, uint8_t channel, uint8_t flags,
                                                      uint16_t *samples);
/** Round-robin CONVERT: channel (phase+i) % n_ch; *phase_io updated for next block. */
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                      uint16_t *samples, uint8_t *phase_io);
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                        uint16_t *samples, uint8_t *phase_io);
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotReadRange(uint32_t n, uint8_t first_ch, uint8_t n_ch,
                                                           uint8_t flags, uint16_t *samples,
                                                           uint8_t *phase_io);

/** Non-blocking TIM-slot stream DMA (USB stream path). */
typedef enum {
  INTAN_STREAM_DMA_IDLE = 0,
  INTAN_STREAM_DMA_RUNNING,
  INTAN_STREAM_DMA_DONE,
  INTAN_STREAM_DMA_ERROR,
} IntanStreamDmaState;

void Intan_StreamDmaReset(void);
HAL_StatusTypeDef Intan_StreamDmaStartSingle(uint32_t n, uint8_t channel, uint8_t flags,
                                             uint16_t *samples);
HAL_StatusTypeDef Intan_StreamDmaStartRange(uint32_t n, uint8_t first_ch, uint8_t n_ch, uint8_t flags,
                                            uint16_t *samples, uint8_t *phase_io);
IntanStreamDmaState Intan_StreamDmaPoll(void);
HAL_StatusTypeDef Intan_StreamDmaComplete(uint8_t halt_after);

/** Conservative round-robin polling path; *phase_io updated for next block. */
HAL_StatusTypeDef Intan_ConvertPipelineSafeReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                  uint16_t *samples, uint8_t *phase_io);
HAL_StatusTypeDef Intan_ConvertPipelineTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                             uint32_t target_ksps, uint16_t *last_value);
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                                uint16_t *last_value);
/** Один 32-битный SPI слот RHS2116 с текущими CS/timing. Для локального pattern executor. */
HAL_StatusTypeDef Intan_Xfer32Word(uint32_t tx_word, uint32_t *rx_out);
HAL_StatusTypeDef Intan_RawCmd(const uint8_t cmd4[4]);
/** READ 255 с M=1 — сброс compliance monitor (как clear_compliance_monitor в Python). */
HAL_StatusTypeDef Intan_ClearComplianceMonitor(void);
HAL_StatusTypeDef Intan_MeasureImpedance(IntanImpedanceArg *arg);
HAL_StatusTypeDef Intan_MeasureImpedanceTimed(const IntanImpedanceTimedArg *arg,
                                              IntanImpedanceTimedResult *result);
/** Останов TIM+DMA+SPI (CS в GPIO) — между STREAM и текстовыми SPI-командами. */
void Intan_DmaPathRelease(void);
/** Непрерывный USB-stream: pipeline primed после 1-го chunk; TIM/SPI без halt между chunk. */
void Intan_SetDmaStreamContinuous(uint8_t enable);
void Intan_SetDmaStreamChannelCount(uint8_t channel_count);

void Intan_SpiStats_Reset(void);
uint32_t Intan_SpiStats_GetXfer32Count(void);
uint32_t Intan_GetLastUnpackRxOffset(void);
uint32_t Intan_DmaSubchunkMax(void);
uint8_t Intan_PipelineChannelIndex(uint8_t phase, uint32_t sample_index, uint32_t rx_offset,
                                   uint8_t n_ch);
void Intan_SpiStats_AddXfer32(uint32_t count);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_SPI_H */
