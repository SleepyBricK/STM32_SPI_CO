/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body — WeAct STM32H743, МК STM32H743VIT6.
  *
  * Контекст для агентов (железо, SPI2/Intan, сборка CMake): см. AGENTS.md в
  * корне репозитория. При потере контекста новый агент должен прочитать AGENTS.md.
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "rtc.h"
#include "spi.h"
#include "usart.h"
#include "gpio.h"
#include "intan_spi.h"
#include "intan_uart_cli.h"
#include "intan_usb_bulk.h"
#include "usb_device.h"
#include "usb3300_ulpi_hw.h"
#include "usbd_conf.h"
#include <string.h>

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MPU_Config(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

static void UART_EarlyAlive(void)
{
  const char msg[] = "\r\nBOOT\r\n";
  (void)HAL_UART_Transmit(&huart1, (uint8_t *)msg, (uint16_t)(sizeof(msg) - 1U), 200U);
}

static void UART_Mark(const char *s)
{
  UART_DebugMark(s);
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MPU Configuration--------------------------------------------------------*/
  MPU_Config();

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */
  UART_EarlyMinInit(64000000U);
  UART_EarlyPrint("\r\nEARLY\r\n");
  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */
  UART_EarlyMinInit(60000000U);
  UART_EarlyPrint("CLK\r\n");
  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  /* USART до RTC: RTC/LSE опциональны (BOARD_HAS_LSE), не блокируют UART/USB */
  MX_USART1_UART_Init();
  UART_DebugMark("[M] post MX_GPIO + MX_USART1\r\n");
  UART_EarlyAlive();
  UART_DebugMark("[M] before USB3300 ULPI init\r\n");
  USB3300_ULPI_HwInit();
  UART_DebugMark("[M] USB3300 ULPI pins/clocks init\r\n");
  if (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) == 0U)
  {
    UART_DebugMark("[M] WARN USB33RDY is not set\r\n");
  }
  UART_DebugMark("[M] before USB_DEVICE_Init\r\n");
  USB_DEVICE_Init();
  UART_DebugMark("[M] USB HS device init\r\n");
  UART_DebugMark("[M] before MX_RTC_Init\r\n");
  MX_RTC_Init();
  UART_DebugMark("[M] after MX_RTC_Init\r\n");
  UART_Mark("\r\n+RTC\r\n");
#if (INTAN_HW_PRESENT == 1)
  UART_DebugMark("[M] before MX_SPI2_Init\r\n");
  MX_SPI2_Init();
  UART_DebugMark("[M] after MX_SPI2_Init\r\n");
  UART_Mark("\r\n+SPI\r\n");
#endif
  /* USER CODE BEGIN 2 */
#if (INTAN_HW_PRESENT == 1)
  UART_DebugMark("[M] before Intan_SPI_Init\r\n");
  Intan_SPI_Init(&hspi2);
  if (Intan_ChipBringup() != HAL_OK)
  {
    UART_DebugMark("[M] Intan_ChipBringup failed (SPI/Intan?)\r\n");
  }
  {
    uint16_t ign = 0U;
    (void)Intan_ReadReg(255U, &ign);
    (void)Intan_ReadReg(255U, &ign);
  }
  UART_DebugMark("[M] after Intan_SPI_Init + bringup\r\n");
  UART_Mark("\r\n+INTAN_GPIO\r\n");
#else
  UART_DebugMark("[M] INTAN_HW_PRESENT=0 — skip SPI2/Intan (USB/UART test mode)\r\n");
#endif
  UART_DebugMark("[M] before Intan_UART_CLI_Init\r\n");
  Intan_UART_CLI_Init();
  UART_DebugMark("[M] after Intan_UART_CLI_Init (main loop)\r\n");
  USB_DEVICE_FinalizeAttach();
  UART_DebugMark("[M] ready: UART HELP/PING | USB3300 replug if rst=0, then usb_intan_cmd.py\r\n");

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    Intan_UART_CLI_Process();
    Intan_USB_Bulk_Process();
    USB_DEVICE_PollEvents();
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /*
   * Как WorkingVER (CDC 5740): VSCALE2, HSE-only, 240 MHz SYSCLK.
   * LSE включается позже в MX_RTC_Init — не блокирует UART/USB при старте.
   */
  HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);

  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE2);

  while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY))
  {
  }

  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 4;
  RCC_OscInitStruct.PLL.PLLN = 240;
  RCC_OscInitStruct.PLL.PLLP = 2;
  RCC_OscInitStruct.PLL.PLLQ = 4;
  RCC_OscInitStruct.PLL.PLLR = 2;
  RCC_OscInitStruct.PLL.PLLRGE = RCC_PLL1VCIRANGE_3;
  RCC_OscInitStruct.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
  RCC_OscInitStruct.PLL.PLLFRACN = 0;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK
                              | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2
                              | RCC_CLOCKTYPE_D3PCLK1 | RCC_CLOCKTYPE_D1PCLK1;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV2;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV2;
  RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV2;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }

  /* PLL2P = 200 MHz для SPI2 Intan (HSE 8 MHz / 2 * 100 / 2). */
  {
    RCC_PeriphCLKInitTypeDef pll2cfg = {0};
    pll2cfg.PeriphClockSelection = RCC_PERIPHCLK_PLL2_DIVP | RCC_PERIPHCLK_PLL2_DIVQ;
    pll2cfg.PLL2.PLL2M = 2;
    pll2cfg.PLL2.PLL2N = 100;
    pll2cfg.PLL2.PLL2P = 2;
    pll2cfg.PLL2.PLL2Q = 4;
    pll2cfg.PLL2.PLL2R = 2;
    pll2cfg.PLL2.PLL2RGE = RCC_PLL2VCIRANGE_2;
    pll2cfg.PLL2.PLL2VCOSEL = RCC_PLL2VCOMEDIUM;
    pll2cfg.PLL2.PLL2FRACN = 0;
    if (HAL_RCCEx_PeriphCLKConfig(&pll2cfg) != HAL_OK)
    {
      Error_Handler();
    }
  }
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

 /* MPU Configuration */

void MPU_Config(void)
{
  MPU_Region_InitTypeDef MPU_InitStruct = {0};

  /* Disables the MPU */
  HAL_MPU_Disable();

  /** Initializes and configures the Region and the memory to be protected
  */
  MPU_InitStruct.Enable = MPU_REGION_ENABLE;
  MPU_InitStruct.Number = MPU_REGION_NUMBER0;
  MPU_InitStruct.BaseAddress = 0x0;
  MPU_InitStruct.Size = MPU_REGION_SIZE_4GB;
  MPU_InitStruct.SubRegionDisable = 0x87;
  MPU_InitStruct.TypeExtField = MPU_TEX_LEVEL0;
  MPU_InitStruct.AccessPermission = MPU_REGION_NO_ACCESS;
  MPU_InitStruct.DisableExec = MPU_INSTRUCTION_ACCESS_DISABLE;
  MPU_InitStruct.IsShareable = MPU_ACCESS_SHAREABLE;
  MPU_InitStruct.IsCacheable = MPU_ACCESS_NOT_CACHEABLE;
  MPU_InitStruct.IsBufferable = MPU_ACCESS_NOT_BUFFERABLE;

  HAL_MPU_ConfigRegion(&MPU_InitStruct);
  /* Enables the MPU */
  HAL_MPU_Enable(MPU_PRIVILEGED_DEFAULT);

}

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  UART_EarlyMinInit(60000000U);
  UART_EarlyPrint("\r\n!ERR_HANDLER\r\n");
  UART_SosBlinkPB6();
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
