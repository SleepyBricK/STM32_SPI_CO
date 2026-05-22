/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "rtc.h"
#include "spi.h"
#include "usart.h"
#include "gpio.h"
#include "usb3300_ulpi_hw.h"
#include "usb_device.h"
#include "uart_diag.h"

extern USBD_HandleTypeDef hUsbDeviceHS;

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
static uint32_t s_diag_loop;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MPU_Config(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

static void UART_BlinkSos(void)
{
  GPIO_InitTypeDef g = {0};
  uint32_t i;

  __HAL_RCC_GPIOB_CLK_ENABLE();
  g.Pin = GPIO_PIN_6;
  g.Mode = GPIO_MODE_OUTPUT_PP;
  g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOB, &g);

  for (;;)
  {
    for (i = 0U; i < 6U; i++)
    {
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_6, GPIO_PIN_SET);
      for (volatile uint32_t d = 0U; d < 200000U; d++) {}
      HAL_GPIO_WritePin(GPIOB, GPIO_PIN_6, GPIO_PIN_RESET);
      for (volatile uint32_t d = 0U; d < 200000U; d++) {}
    }
    for (volatile uint32_t d = 0U; d < 1200000U; d++) {}
  }
}

static void Diag_Step(const char *name)
{
  UART_DiagPrintf("[M] >>> %s (tick=%lu)\r\n", name, (unsigned long)HAL_GetTick());
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

  MPU_Config();
  HAL_Init();
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  Diag_Step("MX_GPIO_Init");
  MX_GPIO_Init();

  Diag_Step("MX_USART1_UART_Init");
  MX_USART1_UART_Init();

  UART_DiagBanner();
  UART_DiagMark("[M] (MPU + HAL + Clock already done — log starts here)\r\n");
  UART_DiagDumpResetCause();
  UART_DiagDumpClocks();

  Diag_Step("USB3300_ULPI_HwInit");
  USB3300_ULPI_HwInit();

  Diag_Step("USB_DEVICE_Init");
  USB_DEVICE_Init();

  Diag_Step("MX_RTC_Init");
  MX_RTC_Init();
  UART_DiagPrintf("[M] LSE ready=%u\r\n",
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_LSERDY));

  Diag_Step("MX_SPI2_Init");
  MX_SPI2_Init();

  /* USER CODE BEGIN 2 */
  UART_DiagMark("[M] === init complete, entering main loop ===\r\n");
  if (hUsbDeviceHS.dev_state == 3U)
  {
    UART_DiagMark("[M] USB OK on MCU. On Mac run:\r\n");
    UART_DiagMark("[M]   system_profiler SPUSBDataType | grep -A8 0483\r\n");
    UART_DiagMark("[M]   ls /dev/cu.usbmodem*\r\n");
    UART_DiagMark("[M] Do NOT use usbserial-* for CDC — that is this debug log.\r\n");
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    s_diag_loop++;
    UART_DiagPrintf("\r\n[LOOP] #%lu tick=%lu\r\n",
                    (unsigned long)s_diag_loop, (unsigned long)HAL_GetTick());
    UART_DiagDumpClocks();
    UART_DiagDumpPwrUsb();
    UART_DiagDumpOtgHsRegs();
    UART_DiagDumpPcd("loop");
    UART_DiagDumpUsbd("loop");
    HAL_Delay(3000);
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
  HAL_StatusTypeDef st;

  HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);

  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE2);

  while(!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {}

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
  st = HAL_RCC_OscConfig(&RCC_OscInitStruct);
  if (st != HAL_OK)
  {
    Error_Handler();
  }

  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2
                              |RCC_CLOCKTYPE_D3PCLK1|RCC_CLOCKTYPE_D1PCLK1;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV2;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV2;
  RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV2;

  st = HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2);
  if (st != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE END 4 */

 /* MPU Configuration */

void MPU_Config(void)
{
  MPU_Region_InitTypeDef MPU_InitStruct = {0};

  HAL_MPU_Disable();

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
  HAL_MPU_Enable(MPU_PRIVILEGED_DEFAULT);

}

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  if (huart1.Instance == USART1)
  {
    UART_DiagMark("\r\n!!! Error_Handler !!!\r\n");
    UART_DiagDumpClocks();
    UART_DiagDumpPwrUsb();
    UART_DiagDumpOtgHsRegs();
    UART_DiagDumpPcd("err");
    UART_DiagDumpUsbd("err");
  }
  UART_BlinkSos();
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
  UART_DiagPrintf("ASSERT %s:%lu\r\n", file, (unsigned long)line);
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
