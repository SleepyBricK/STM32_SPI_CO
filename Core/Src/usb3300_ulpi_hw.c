#include "usb3300_ulpi_hw.h"
#include "main.h"

#ifndef USB_VDD33_BYPASS
#define USB_VDD33_BYPASS  1U
#endif

#ifndef USB3300_XTAL_STARTUP_MS
#define USB3300_XTAL_STARTUP_MS  10U
#endif

static uint8_t s_done;

static void USB_ULPI_ClockInit(void)
{
  RCC_PeriphCLKInitTypeDef clk = {0};

  clk.PeriphClockSelection = RCC_PERIPHCLK_USB;
  clk.PLL3.PLL3M = 4;
  clk.PLL3.PLL3N = 96;
  clk.PLL3.PLL3P = 2;
  clk.PLL3.PLL3Q = 4;
  clk.PLL3.PLL3R = 2;
  clk.PLL3.PLL3RGE = RCC_PLL3VCIRANGE_1;
  clk.PLL3.PLL3VCOSEL = RCC_PLL3VCOWIDE;
  clk.PLL3.PLL3FRACN = 0;
  clk.UsbClockSelection = RCC_USBCLKSOURCE_PLL3;

  if (HAL_RCCEx_PeriphCLKConfig(&clk) != HAL_OK)
  {
    Error_Handler();
  }
}

static void USB_PowerInit(void)
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
      return;
    }
  }
#else
  if (HAL_PWREx_EnableUSBReg() != HAL_OK)
  {
    (void)HAL_PWREx_DisableUSBReg();
    HAL_PWREx_EnableUSBVoltageDetector();
  }

  t0 = HAL_GetTick();
  while (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) == 0U)
  {
    if ((HAL_GetTick() - t0) > 500U)
    {
      return;
    }
  }
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

void USB3300_ULPI_HwInit(void)
{
  if (s_done != 0U)
  {
    return;
  }

  HAL_Delay(USB3300_XTAL_STARTUP_MS);
  USB_ULPI_ClockInit();
  USB_PowerInit();
  USB3300_ULPI_GpioInit();
  s_done = 1U;
}
