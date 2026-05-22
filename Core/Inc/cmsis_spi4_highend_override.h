/**
 * @file cmsis_spi4_highend_override.h
 * @brief Подключать сразу после #include "stm32h7xx_hal_spi.h" в stm32h7xx_hal_conf.h.
 *
 * В stm32h743xx.h макрос IS_SPI_HIGHEND_INSTANCE задаёт только SPI1–SPI3. При этом SPI4 на H7
 * поддерживает 32-битный кадр (CFG1 DSIZE); HAL_SPI_Init иначе возвращает HAL_ERROR до HAL_SPI_MspInit.
 */
#ifndef CMSIS_SPI4_HIGHEND_OVERRIDE_H
#define CMSIS_SPI4_HIGHEND_OVERRIDE_H

#undef IS_SPI_HIGHEND_INSTANCE
#define IS_SPI_HIGHEND_INSTANCE(INSTANCE) (((INSTANCE) == SPI1) || ((INSTANCE) == SPI2) || \
                                           ((INSTANCE) == SPI3) || ((INSTANCE) == SPI4))

#endif /* CMSIS_SPI4_HIGHEND_OVERRIDE_H */
