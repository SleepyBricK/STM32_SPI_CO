#include "usb3300_ulpi_hw.h"
#include "usart.h"
#include <stdio.h>

/*
 * STM32H743VIT6 / LQFP100: отдельного pin VDD33USB нет — домен USB 3.3 V
 * питается от VDD (~3.3 V). Внутренний USB LDO (USBREGEN) не используем (bypass).
 */
#ifndef USB_VDD33_BYPASS
#define USB_VDD33_BYPASS  1U
#endif

static uint8_t s_ulpi_hw_done;

static HAL_StatusTypeDef USB_PowerInit(void)
{
  uint32_t t0;

#if (USB_VDD33_BYPASS == 1U)
  (void)HAL_PWREx_DisableUSBReg();
  HAL_PWREx_EnableUSBVoltageDetector();

  t0 = HAL_GetTick();
  while (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) == 0U)
  {
    if ((HAL_GetTick() - t0) > 500U)
    {
      char line[72];
      (void)snprintf(line, sizeof(line),
                     "[usb] bypass: USB33RDY=0 CR3=0x%08lX (VDD ok?)\r\n",
                     (unsigned long)PWR->CR3);
      UART_Log(line);
      UART_Log("[usb] bypass: continue OTG init anyway\r\n");
      return HAL_OK;
    }
  }

  UART_Log("[usb] power: VDD33 bypass USB33RDY=1\r\n");
  return HAL_OK;
#else
  if (HAL_PWREx_EnableUSBReg() == HAL_OK)
  {
    HAL_PWREx_EnableUSBVoltageDetector();
    UART_Log("[usb] power: internal USB LDO OK\r\n");
    return HAL_OK;
  }

  UART_Log("[usb] power: LDO fail, try bypass\r\n");
  (void)HAL_PWREx_DisableUSBReg();
  HAL_PWREx_EnableUSBVoltageDetector();

  t0 = HAL_GetTick();
  while (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) == 0U)
  {
    if ((HAL_GetTick() - t0) > 500U)
    {
      return HAL_ERROR;
    }
  }

  UART_Log("[usb] power: bypass USB33RDY=1\r\n");
  return HAL_OK;
#endif
}

static void USB3300_ULPI_GpioInit(void)
{
  GPIO_InitTypeDef gpio = {0};

  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();

  HAL_SYSCFG_AnalogSwitchConfig(SYSCFG_SWITCH_PC2, SYSCFG_SWITCH_PC2_CLOSE);
  HAL_SYSCFG_AnalogSwitchConfig(SYSCFG_SWITCH_PC3, SYSCFG_SWITCH_PC3_CLOSE);

  gpio.Mode = GPIO_MODE_AF_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  gpio.Alternate = GPIO_AF10_OTG1_HS;

  gpio.Pin = GPIO_PIN_3 | GPIO_PIN_5;
  HAL_GPIO_Init(GPIOA, &gpio);

  gpio.Pin = GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_5 | GPIO_PIN_10 |
             GPIO_PIN_11 | GPIO_PIN_12 | GPIO_PIN_13;
  HAL_GPIO_Init(GPIOB, &gpio);

  gpio.Pin = GPIO_PIN_0 | GPIO_PIN_2 | GPIO_PIN_3;
  HAL_GPIO_Init(GPIOC, &gpio);

  __HAL_RCC_USB1_OTG_HS_CLK_ENABLE();
  __HAL_RCC_USB1_OTG_HS_ULPI_CLK_ENABLE();
}

HAL_StatusTypeDef USB3300_ULPI_HwInit(void)
{
  if (s_ulpi_hw_done != 0U)
  {
    return HAL_OK;
  }

  (void)USB_PowerInit();
  USB3300_ULPI_GpioInit();
  s_ulpi_hw_done = 1U;

  return HAL_OK;
}
