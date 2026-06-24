#include "iwdg.h"

#define IWDG_BACKUP_MAGIC       0x49574447UL
#define IWDG_BACKUP_WAS_RESET   0x00000001UL
#define IWDG_RELOAD_3S          375U
#define IWDG_REFRESH_PERIOD_MS  250U

IWDG_HandleTypeDef hiwdg1;
static uint8_t s_iwdg_reset;
static uint32_t s_last_refresh_tick;

static void iwdg_backup_write(uint32_t value)
{
  HAL_PWR_EnableBkUpAccess();
  RTC->BKP0R = value;
}

void Iwdg_CaptureResetCause(void)
{
  s_iwdg_reset = ((RCC->RSR & RCC_RSR_IWDG1RSTF) != 0U) ? 1U : 0U;
  iwdg_backup_write(IWDG_BACKUP_MAGIC | (s_iwdg_reset != 0U ? IWDG_BACKUP_WAS_RESET : 0U));
  __HAL_RCC_CLEAR_RESET_FLAGS();
}

uint8_t Iwdg_WasReset(void)
{
  return s_iwdg_reset;
}

void MX_IWDG_Init(void)
{
  hiwdg1.Instance = IWDG1;
  hiwdg1.Init.Prescaler = IWDG_PRESCALER_256;
  hiwdg1.Init.Reload = IWDG_RELOAD_3S;
  hiwdg1.Init.Window = IWDG_WINDOW_DISABLE;
  if (HAL_IWDG_Init(&hiwdg1) != HAL_OK)
  {
    Error_Handler();
  }
  s_last_refresh_tick = HAL_GetTick();
}

void Iwdg_RefreshIfHealthy(uint8_t healthy)
{
  uint32_t now;

  if (healthy == 0U)
  {
    return;
  }

  now = HAL_GetTick();
  if ((now - s_last_refresh_tick) >= IWDG_REFRESH_PERIOD_MS)
  {
    (void)HAL_IWDG_Refresh(&hiwdg1);
    s_last_refresh_tick = now;
  }
}
