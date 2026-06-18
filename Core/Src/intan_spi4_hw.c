/*
 * SPI2/SPI4 без HAL_SPI_TransmitReceive / HAL_SPI_Init для Intan RHS2116.
 * Логика регистров — эквивалент HAL_SPI_Init / HAL_SPI_TransmitReceive(polling).
 */

#include "intan_spi4_hw.h"

static HAL_StatusTypeDef intan_waituntil_ms(uint32_t (*cond)(void *), void *ctx, uint32_t timeout_ms);

static uint32_t flag_txp_ready(void *p)
{
  SPI_TypeDef *SPIx = (SPI_TypeDef *)p;
  return ((SPIx->SR & SPI_SR_TXP) != 0U) ? 1U : 0U;
}

static uint32_t flag_rxp_ready(void *p)
{
  SPI_TypeDef *SPIx = (SPI_TypeDef *)p;
  return ((SPIx->SR & SPI_SR_RXP) != 0U) ? 1U : 0U;
}

static uint32_t flag_eot_set(void *p)
{
  SPI_TypeDef *SPIx = (SPI_TypeDef *)p;
  return ((SPIx->SR & SPI_SR_EOT) != 0U) ? 1U : 0U;
}

static HAL_StatusTypeDef intan_waituntil_ms(uint32_t (*cond)(void *), void *ctx, uint32_t timeout_ms)
{
  uint32_t t0 = HAL_GetTick();
  while (cond(ctx) == 0U)
  {
    if (((HAL_GetTick() - t0) >= timeout_ms) && (timeout_ms != HAL_MAX_DELAY))
    {
      return HAL_TIMEOUT;
    }
  }
  return HAL_OK;
}

static void intan_spi4_close_transfer(SPI_TypeDef *SPIx)
{
  SPIx->IFCR |= SPI_IFCR_EOTC | SPI_IFCR_TXTFC;
  CLEAR_BIT(SPIx->CR1, SPI_CR1_SPE);
  SPIx->IER = 0U;
  CLEAR_BIT(SPIx->CFG1, SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
}

HAL_StatusTypeDef Intan_SPI4_HwInit(SPI_HandleTypeDef *hspi)
{
  uint32_t crc_length;

  if (hspi == NULL || hspi->Instance == NULL)
  {
    return HAL_ERROR;
  }

  crc_length = hspi->Instance->CFG1 & SPI_CFG1_CRCSIZE;

  CLEAR_BIT(hspi->Instance->CR1, SPI_CR1_SPE);

  if ((hspi->Init.NSS == SPI_NSS_SOFT) && (hspi->Init.Mode == SPI_MODE_MASTER) &&
      (hspi->Init.NSSPolarity == SPI_NSS_POLARITY_LOW))
  {
    SET_BIT(hspi->Instance->CR1, SPI_CR1_SSI);
  }
  else
  {
    CLEAR_BIT(hspi->Instance->CR1, SPI_CR1_SSI);
  }

  if ((hspi->Init.Mode & SPI_MODE_MASTER) == SPI_MODE_MASTER)
  {
    MODIFY_REG(hspi->Instance->CR1, SPI_CR1_MASRX, hspi->Init.MasterReceiverAutoSusp);
  }
  else
  {
    CLEAR_BIT(hspi->Instance->CR1, SPI_CR1_MASRX);
  }

  WRITE_REG(hspi->Instance->CFG1, (hspi->Init.BaudRatePrescaler | hspi->Init.CRCCalculation | crc_length |
                                     hspi->Init.FifoThreshold | hspi->Init.DataSize));

  WRITE_REG(hspi->Instance->CFG2, (hspi->Init.NSSPMode | hspi->Init.TIMode | hspi->Init.NSSPolarity |
                                     hspi->Init.NSS | hspi->Init.CLKPolarity | hspi->Init.CLKPhase |
                                     hspi->Init.FirstBit | hspi->Init.Mode | hspi->Init.MasterInterDataIdleness |
                                     hspi->Init.Direction | hspi->Init.MasterSSIdleness | hspi->Init.IOSwap));

  if ((hspi->Init.Mode & SPI_MODE_MASTER) == SPI_MODE_MASTER)
  {
    MODIFY_REG(hspi->Instance->CFG2, SPI_CFG2_AFCNTR, hspi->Init.MasterKeepIOState);
  }

#if defined(SPI_I2SCFGR_I2SMOD)
  CLEAR_BIT(hspi->Instance->I2SCFGR, SPI_I2SCFGR_I2SMOD);
#endif

  hspi->ErrorCode = HAL_SPI_ERROR_NONE;
  hspi->State = HAL_SPI_STATE_READY;
  return HAL_OK;
}

HAL_StatusTypeDef Intan_SPI4_Transfer32(SPI_TypeDef *SPIx, uint32_t tx_word, uint32_t *rx_word, uint32_t timeout_ms)
{
  if (SPIx == NULL || rx_word == NULL)
  {
    return HAL_ERROR;
  }

  /* Full-duplex 2-line (как SPI_2LINES в HAL). */
  MODIFY_REG(SPIx->CFG2, SPI_CFG2_COMM, 0U);

  MODIFY_REG(SPIx->CR2, SPI_CR2_TSIZE, 1U);
  SPIx->IFCR |= SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

  __IO uint32_t *txdr32 = (__IO uint32_t *)&SPIx->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&SPIx->RXDR;

  SET_BIT(SPIx->CR1, SPI_CR1_SPE);

  SET_BIT(SPIx->CR1, SPI_CR1_CSTART);

  if (intan_waituntil_ms(flag_txp_ready, SPIx, timeout_ms) != HAL_OK)
  {
    intan_spi4_close_transfer(SPIx);
    return HAL_TIMEOUT;
  }

  *txdr32 = tx_word;

  if (intan_waituntil_ms(flag_rxp_ready, SPIx, timeout_ms) != HAL_OK)
  {
    intan_spi4_close_transfer(SPIx);
    return HAL_TIMEOUT;
  }
  *rx_word = *rxdr32;

  if (intan_waituntil_ms(flag_eot_set, SPIx, timeout_ms) != HAL_OK)
  {
    intan_spi4_close_transfer(SPIx);
    return HAL_TIMEOUT;
  }

  intan_spi4_close_transfer(SPIx);
  return HAL_OK;
}
