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
#define INTAN_STREAM_RR8_CHANNELS      8U

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples);
/** Round-robin CONVERT: channel (phase+i) % n_ch; *phase_io updated for next block. */
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                      uint16_t *samples, uint8_t *phase_io);
HAL_StatusTypeDef Intan_ConvertPipelineTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                             uint32_t target_ksps, uint16_t *last_value);
HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                                uint16_t *last_value);
HAL_StatusTypeDef Intan_RawCmd(const uint8_t cmd4[4]);
/** READ 255 с M=1 — сброс compliance monitor (как clear_compliance_monitor в Python). */
HAL_StatusTypeDef Intan_ClearComplianceMonitor(void);
HAL_StatusTypeDef Intan_MeasureImpedance(IntanImpedanceArg *arg);
/** Останов TIM+DMA+SPI (CS в GPIO) — между STREAM и текстовыми SPI-командами. */
void Intan_DmaPathRelease(void);

void Intan_SpiStats_Reset(void);
uint32_t Intan_SpiStats_GetXfer32Count(void);
void Intan_SpiStats_AddXfer32(uint32_t count);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_SPI_H */
