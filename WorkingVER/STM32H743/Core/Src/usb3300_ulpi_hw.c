#include "usb3300_ulpi_hw.h"
#include "main.h"
#include "uart_diag.h"

/*
 * Аппаратный чеклист: USB3300-Hardware-Design-Checklist-00002886A.pdf (Microchip).
 *
 * Device (peripheral), не host/OTG:
 * - 24.000 MHz ±500 ppm на XI/XO (или CLKIN 3.3 V, XO = NC)
 * - RBIAS → GND через 12 kΩ ±1%
 * - VDD33 = 3.3 V; REG_EN + 4.7 µF на VDD18/VDDA18 при внутреннем LDO
 * - CVBUS = 1 µF у разъёма (не 120 µF как у host)
 * - DP/DM: Zdiff 90 Ω, Z0 40–55 Ω
 * - ID — не подключать / float (peripheral)
 * - RESET — GND или pulse ≥1 CLKOUT; внутренний pull-down
 * - ULPI: короткие дорожки, без stub/конденсаторов на линиях
 *
 * STM32H743VIT6: отдельного VDD33USB нет — USB домен от VDD, USBREGEN bypass.
 */
#ifndef USB_VDD33_BYPASS
#define USB_VDD33_BYPASS  1U
#endif

/* Время стабилизации 24 MHz кварца USB3300 после power-up (DS00002886A, §5–7). */
#ifndef USB3300_XTAL_STARTUP_MS
#define USB3300_XTAL_STARTUP_MS  10U
#endif

/*
 * Опционально: RESET USB3300 на GPIO (active high).
 * В usb3300_ulpi_hw.h или -D: Port, Pin, USB3300_RESET_GPIO_CLK_ENABLE().
 */
#if defined(USB3300_RESET_GPIO_Port) && defined(USB3300_RESET_GPIO_Pin) && defined(USB3300_RESET_GPIO_CLK_ENABLE)
static void USB3300_PhyResetPulse(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};

  USB3300_RESET_GPIO_CLK_ENABLE();
  GPIO_InitStruct.Pin = USB3300_RESET_GPIO_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(USB3300_RESET_GPIO_Port, &GPIO_InitStruct);

  HAL_GPIO_WritePin(USB3300_RESET_GPIO_Port, USB3300_RESET_GPIO_Pin, GPIO_PIN_RESET);
  HAL_Delay(2U);
  HAL_GPIO_WritePin(USB3300_RESET_GPIO_Port, USB3300_RESET_GPIO_Pin, GPIO_PIN_SET);
  /* ≥1 период CLKOUT (24 MHz) + 2 такта до смены ULPI — 2 ms с запасом */
  HAL_Delay(2U);
}
#endif

static uint8_t s_ulpi_hw_done;

static HAL_StatusTypeDef USB_PowerInit(void)
{
  uint32_t t0;

#if (USB_VDD33_BYPASS == 1U)
  UART_DiagMark("[ULPI] PWR: DisableUSBReg + EnableUSBVoltageDetector\r\n");
  (void)HAL_PWREx_DisableUSBReg();
  HAL_PWREx_EnableUSBVoltageDetector();

  t0 = HAL_GetTick();
  while (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) == 0U)
  {
    if ((HAL_GetTick() - t0) > 500U)
    {
      UART_DiagMark("[ULPI] PWR: WARN USB33RDY timeout 500ms, continue\r\n");
      return HAL_OK;
    }
  }

  UART_DiagMark("[ULPI] PWR: USB33RDY=1\r\n");
  return HAL_OK;
#else
  if (HAL_PWREx_EnableUSBReg() == HAL_OK)
  {
    HAL_PWREx_EnableUSBVoltageDetector();
    return HAL_OK;
  }

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

  return HAL_OK;
#endif
}

static void USB_ULPI_ClockInit(void)
{
  RCC_PeriphCLKInitTypeDef PeriphClkInitStruct = {0};
  HAL_StatusTypeDef st;

  PeriphClkInitStruct.PeriphClockSelection = RCC_PERIPHCLK_USB;
  PeriphClkInitStruct.PLL3.PLL3M = 4;
  PeriphClkInitStruct.PLL3.PLL3N = 96;
  PeriphClkInitStruct.PLL3.PLL3P = 2;
  PeriphClkInitStruct.PLL3.PLL3Q = 4;
  PeriphClkInitStruct.PLL3.PLL3R = 2;
  PeriphClkInitStruct.PLL3.PLL3RGE = RCC_PLL3VCIRANGE_1;
  PeriphClkInitStruct.PLL3.PLL3VCOSEL = RCC_PLL3VCOWIDE;
  PeriphClkInitStruct.PLL3.PLL3FRACN = 0;
  PeriphClkInitStruct.UsbClockSelection = RCC_USBCLKSOURCE_PLL3;
  st = HAL_RCCEx_PeriphCLKConfig(&PeriphClkInitStruct);
  UART_DiagPrintf("[ULPI] RCC USB PLL3 config: %s\r\n", (st == HAL_OK) ? "OK" : "FAIL");
  if (st != HAL_OK)
  {
    Error_Handler();
  }
  UART_DiagDumpRccUsbPll3();
}

static void USB3300_ULPI_GpioInit(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};

  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();

  /* PC2_C/PC3_C подключены к PC2/PC3 через analog switch. */
  HAL_SYSCFG_AnalogSwitchConfig(SYSCFG_SWITCH_PC2, SYSCFG_SWITCH_PC2_CLOSE);
  HAL_SYSCFG_AnalogSwitchConfig(SYSCFG_SWITCH_PC3, SYSCFG_SWITCH_PC3_CLOSE);

  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  GPIO_InitStruct.Alternate = GPIO_AF10_OTG1_HS;

  GPIO_InitStruct.Pin = GPIO_PIN_3 | GPIO_PIN_5;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  GPIO_InitStruct.Pin = GPIO_PIN_0 | GPIO_PIN_1 | GPIO_PIN_5 | GPIO_PIN_10 |
                        GPIO_PIN_11 | GPIO_PIN_12 | GPIO_PIN_13;
  HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

  GPIO_InitStruct.Pin = GPIO_PIN_0 | GPIO_PIN_2 | GPIO_PIN_3;
  HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);

  __HAL_RCC_USB1_OTG_HS_CLK_ENABLE();
  __HAL_RCC_USB1_OTG_HS_ULPI_CLK_ENABLE();
}

void USB3300_ULPI_HwInit(void)
{
  if (s_ulpi_hw_done != 0U)
  {
    UART_DiagMark("[ULPI] skip: already initialized\r\n");
    return;
  }

  UART_DiagPrintf("[ULPI] === USB3300 ULPI HwInit start (tick=%lu) ===\r\n",
                  (unsigned long)HAL_GetTick());

#if defined(USB3300_RESET_GPIO_Port) && defined(USB3300_RESET_GPIO_Pin) && defined(USB3300_RESET_GPIO_CLK_ENABLE)
  UART_DiagMark("[ULPI] RESET pulse on GPIO\r\n");
  USB3300_PhyResetPulse();
#else
  UART_DiagPrintf("[ULPI] wait USB3300 XTAL %u ms (no GPIO RESET)\r\n",
                  (unsigned)USB3300_XTAL_STARTUP_MS);
  HAL_Delay(USB3300_XTAL_STARTUP_MS);
#endif

  USB_ULPI_ClockInit();
  (void)USB_PowerInit();
  UART_DiagMark("[ULPI] GPIO: PA3/PA5 PB0/1/5/10-13 PC0/2/3 AF10_OTG1_HS\r\n");
  USB3300_ULPI_GpioInit();
  UART_DiagMark("[ULPI] analog switch PC2/PC3 closed\r\n");
  UART_DiagDumpPwrUsb();
  s_ulpi_hw_done = 1U;
  UART_DiagMark("[ULPI] === USB3300 ULPI HwInit done ===\r\n");
}
