#include "intan_pattern.h"
#include "intan_spi.h"
#include "stm32h7xx.h"

static IntanPatternSlot s_slots[INTAN_PATTERN_MAX_SLOTS];
static IntanPatternStatus s_status;

static inline uint32_t pack_be4(uint8_t b0, uint8_t b1, uint8_t b2, uint8_t b3)
{
  return ((uint32_t)b0 << 24) | ((uint32_t)b1 << 16) | ((uint32_t)b2 << 8) | (uint32_t)b3;
}

static void dwt_enable(void)
{
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static void delay_cycles_busy(uint32_t cycles)
{
  uint32_t start;

  if (cycles == 0U)
  {
    return;
  }

  dwt_enable();
  start = DWT->CYCCNT;
  while ((uint32_t)(DWT->CYCCNT - start) < cycles)
  {
    __NOP();
  }
}

static HAL_StatusTypeDef reserve_slots(uint32_t needed)
{
  if (s_status.running != 0U)
  {
    return HAL_BUSY;
  }
  if (needed > (INTAN_PATTERN_MAX_SLOTS - s_status.slot_count))
  {
    s_status.last_error = 1U;
    return HAL_ERROR;
  }
  return HAL_OK;
}

static HAL_StatusTypeDef append_slot(uint8_t kind, uint32_t arg)
{
  if (reserve_slots(1U) != HAL_OK)
  {
    return HAL_ERROR;
  }

  s_slots[s_status.slot_count].kind = kind;
  s_slots[s_status.slot_count].arg = arg;
  s_status.slot_count++;
  s_status.loaded = (s_status.slot_count > 0U) ? 1U : 0U;

  if (kind == INTAN_PATTERN_SLOT_SPI)
  {
    s_status.spi_slots++;
  }
  else
  {
    s_status.delay_slots++;
  }

  return HAL_OK;
}

void Intan_Pattern_Clear(void)
{
  if (s_status.running != 0U)
  {
    return;
  }

  s_status.slot_count = 0U;
  s_status.spi_slots = 0U;
  s_status.delay_slots = 0U;
  s_status.loaded = 0U;
  s_status.last_error = 0U;
}

HAL_StatusTypeDef Intan_Pattern_AddRawWord(uint32_t word)
{
  return append_slot(INTAN_PATTERN_SLOT_SPI, word);
}

HAL_StatusTypeDef Intan_Pattern_AddWrite(uint8_t reg_addr, uint16_t value, uint8_t u_flag, uint8_t m_flag)
{
  HAL_StatusTypeDef st;
  uint8_t h = (uint8_t)(0x80U | ((u_flag & 1U) << 5) | ((m_flag & 1U) << 4));

  st = reserve_slots(3U);
  if (st != HAL_OK)
  {
    return st;
  }

  if (((reg_addr >= 64U && reg_addr <= 79U) || (reg_addr >= 96U && reg_addr <= 111U)) &&
      (value < 0x8000U || value > 0x80FFU))
  {
    if (value > 255U)
    {
      s_status.last_error = 4U;
      return HAL_ERROR;
    }
    value = (uint16_t)(0x8000U | (value & 0x00FFU));
  }

  st = append_slot(INTAN_PATTERN_SLOT_SPI,
                   pack_be4(h, reg_addr, (uint8_t)(value >> 8), (uint8_t)(value & 0xFFU)));
  if (st != HAL_OK)
  {
    return st;
  }
  st = append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
}

HAL_StatusTypeDef Intan_Pattern_AddRead(uint8_t reg_addr)
{
  HAL_StatusTypeDef st;

  st = reserve_slots(3U);
  if (st != HAL_OK)
  {
    return st;
  }

  st = append_slot(INTAN_PATTERN_SLOT_SPI, pack_be4(0xC0U, reg_addr, 0x00U, 0x00U));
  if (st != HAL_OK)
  {
    return st;
  }
  st = append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
}

HAL_StatusTypeDef Intan_Pattern_AddConvert(uint8_t channel, uint8_t flags)
{
  HAL_StatusTypeDef st;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);

  if (channel > 63U)
  {
    s_status.last_error = 5U;
    return HAL_ERROR;
  }

  st = reserve_slots(3U);
  if (st != HAL_OK)
  {
    return st;
  }

  st = append_slot(INTAN_PATTERN_SLOT_SPI,
                   pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                            0x00U, 0x00U));
  if (st != HAL_OK)
  {
    return st;
  }
  st = append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
}

HAL_StatusTypeDef Intan_Pattern_AddClearAdc(void)
{
  HAL_StatusTypeDef st;

  st = reserve_slots(3U);
  if (st != HAL_OK)
  {
    return st;
  }

  st = append_slot(INTAN_PATTERN_SLOT_SPI, pack_be4(0x6AU, 0x00U, 0x00U, 0x00U));
  if (st != HAL_OK)
  {
    return st;
  }
  st = append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
}

HAL_StatusTypeDef Intan_Pattern_AddClearCompliance(void)
{
  HAL_StatusTypeDef st;

  st = reserve_slots(3U);
  if (st != HAL_OK)
  {
    return st;
  }

  st = append_slot(INTAN_PATTERN_SLOT_SPI, pack_be4(0xD0U, 255U, 0x00U, 0x00U));
  if (st != HAL_OK)
  {
    return st;
  }
  st = append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return append_slot(INTAN_PATTERN_SLOT_SPI, 0U);
}

HAL_StatusTypeDef Intan_Pattern_AddDelayCycles(uint32_t cycles)
{
  return append_slot(INTAN_PATTERN_SLOT_DELAY_CYCLES, cycles);
}

HAL_StatusTypeDef Intan_Pattern_AddDelayUs(uint32_t us)
{
  return append_slot(INTAN_PATTERN_SLOT_DELAY_US, us);
}

HAL_StatusTypeDef Intan_Pattern_Run(uint32_t repeat_count)
{
  uint32_t r;
  uint32_t i;
  HAL_StatusTypeDef st;

  if (s_status.loaded == 0U || s_status.slot_count == 0U || repeat_count == 0U)
  {
    s_status.last_error = 6U;
    return HAL_ERROR;
  }

  Intan_DmaPathRelease();

  s_status.running = 1U;
  s_status.last_error = 0U;
  dwt_enable();

  for (r = 0U; r < repeat_count; r++)
  {
    for (i = 0U; i < s_status.slot_count; i++)
    {
      const IntanPatternSlot *slot = &s_slots[i];

      if (slot->kind == INTAN_PATTERN_SLOT_SPI)
      {
        st = Intan_Xfer32Word(slot->arg, NULL);
        if (st != HAL_OK)
        {
          s_status.running = 0U;
          s_status.last_error = 2U;
          return st;
        }
      }
      else if (slot->kind == INTAN_PATTERN_SLOT_DELAY_CYCLES)
      {
        delay_cycles_busy(slot->arg);
      }
      else if (slot->kind == INTAN_PATTERN_SLOT_DELAY_US)
      {
        uint64_t cycles = (uint64_t)slot->arg * (uint64_t)(SystemCoreClock / 1000000U);
        delay_cycles_busy((cycles > 0xFFFFFFFFULL) ? 0xFFFFFFFFU : (uint32_t)cycles);
      }
      else
      {
        s_status.running = 0U;
        s_status.last_error = 3U;
        return HAL_ERROR;
      }
    }
  }

  /* Full 3-slot WRITE R42=0 U=1 — raw OFF в паттерне иногда не гасит выход. */
  st = Intan_WriteReg(42U, 0x0000U, 1U, 0U);
  if (st != HAL_OK)
  {
    s_status.running = 0U;
    s_status.last_error = 7U;
    return st;
  }

  s_status.running = 0U;
  return HAL_OK;
}

void Intan_Pattern_GetStatus(IntanPatternStatus *status)
{
  if (status != NULL)
  {
    *status = s_status;
  }
}
