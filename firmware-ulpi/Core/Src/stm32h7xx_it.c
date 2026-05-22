#include "main.h"
#include "stm32h7xx_it.h"
#include "usart.h"

extern PCD_HandleTypeDef hpcd_USB_OTG_HS;

void SysTick_Handler(void)
{
  HAL_IncTick();
}

void OTG_HS_IRQHandler(void)
{
  HAL_PCD_IRQHandler(&hpcd_USB_OTG_HS);
}
