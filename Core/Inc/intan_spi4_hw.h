/**
 * SPI2/SPI4 для Intan RHS2116: конфигурация и 32-бит обмен без HAL_SPI_* API.
 * Часы/GPIO — по-прежнему в HAL_SPI_MspInit (spi.c).
 */

#ifndef INTAN_SPI4_HW_H
#define INTAN_SPI4_HW_H

#include "main.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Те же поля Init, что были в MX_SPIx_Init + HAL_SPI_Init. */
HAL_StatusTypeDef Intan_SPI4_HwInit(SPI_HandleTypeDef *hspi);

/** Один 32-бит кадр full-duplex (как HAL_SPI_TransmitReceive Size=1, 32-bit). */
HAL_StatusTypeDef Intan_SPI4_Transfer32(SPI_TypeDef *SPIx, uint32_t tx_word, uint32_t *rx_word, uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_SPI4_HW_H */
