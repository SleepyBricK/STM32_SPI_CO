/**
 * @file intan_app.c
 */

#include "intan_app.h"
#include "stm32h7xx.h"
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#define INTAN_APP_FAST_ADC_KSPS 610U

#define INTAN_RECORD_REG4  0x0016U
#define INTAN_RECORD_REG5  0x0017U
#define INTAN_RECORD_REG6  0x00A8U
#define INTAN_RECORD_REG7  0x000AU

static void delay_post(void)
{
  HAL_Delay(1);
}

void Intan_App_DWT_Reset(void)
{
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->CYCCNT = 0U;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static void delay_us_busy(uint32_t us)
{
  uint32_t start = DWT->CYCCNT;
  uint32_t ticks = us * (SystemCoreClock / 1000000U);
  if (ticks == 0U)
  {
    ticks = 1U;
  }
  while ((DWT->CYCCNT - start) < ticks)
  {
    __NOP();
  }
}

static uint16_t adc_reg0_from_ksps(uint16_t ksps)
{
  unsigned adc_bb;
  unsigned mux_b;

  if (ksps <= 120U)
  {
    adc_bb = 32U;
    mux_b = 40U;
  }
  else if (ksps <= 140U)
  {
    adc_bb = 16U;
    mux_b = 40U;
  }
  else if (ksps <= 175U)
  {
    adc_bb = 8U;
    mux_b = 40U;
  }
  else if (ksps <= 220U)
  {
    adc_bb = 8U;
    mux_b = 32U;
  }
  else if (ksps <= 280U)
  {
    adc_bb = 8U;
    mux_b = 26U;
  }
  else if (ksps <= 350U)
  {
    adc_bb = 4U;
    mux_b = 18U;
  }
  else if (ksps <= 440U)
  {
    adc_bb = 3U;
    mux_b = 16U;
  }
  else
  {
    adc_bb = 3U;
    mux_b = 5U;
  }

  return (uint16_t)((adc_bb << 8) | mux_b);
}

HAL_StatusTypeDef Intan_App_ClearAdc(void)
{
  const uint8_t clear_cmd[4] = {0x6AU, 0x00U, 0x00U, 0x00U};
  return Intan_RawCmd(clear_cmd);
}

HAL_StatusTypeDef Intan_App_ClearCompliance(void)
{
  return Intan_ClearComplianceMonitor();
}

HAL_StatusTypeDef Intan_App_InitStim(void)
{
  HAL_StatusTypeDef st;
  uint16_t dummy;
  uint16_t r0 = adc_reg0_from_ksps(INTAN_APP_FAST_ADC_KSPS);

  st = Intan_ReadReg(255U, &dummy);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(32U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(33U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(38U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_App_ClearAdc();
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(0U, r0, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(1U, 0x051AU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(2U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(3U, 0x0080U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(4U, 0x0016U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(5U, 0x0017U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(6U, 0x00A8U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(7U, 0x000AU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(8U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(10U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(12U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(34U, 0x00E2U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(35U, 0x00AAU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(36U, 0x0080U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(37U, 0x4F00U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(42U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(44U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(46U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(48U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  for (uint8_t ch = 0U; ch < 16U; ch++)
  {
    st = Intan_WriteReg((uint8_t)(64U + ch), 0x8000U, 0U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
    st = Intan_WriteReg((uint8_t)(96U + ch), 0x8000U, 0U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
  }

  st = Intan_WriteReg(42U, 0x0000U, 1U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(32U, 0xAAAAU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(33U, 0x00FFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_InitRecord(uint16_t adc_ksps)
{
  HAL_StatusTypeDef st;
  uint16_t dummy;
  uint16_t r0 = adc_reg0_from_ksps(adc_ksps);

  st = Intan_ReadReg(255U, &dummy);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(32U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(33U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(38U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_App_ClearAdc();
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(0U, r0, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(1U, 0x051AU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(2U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(3U, 0x0080U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(4U, INTAN_RECORD_REG4, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(5U, INTAN_RECORD_REG5, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(6U, INTAN_RECORD_REG6, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(7U, INTAN_RECORD_REG7, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(8U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(10U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(12U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(44U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(46U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();
  st = Intan_WriteReg(48U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_WriteReg(42U, 0x0000U, 1U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  delay_post();

  st = Intan_App_ClearCompliance();
  if (st != HAL_OK)
  {
    return st;
  }

  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_SetStimMagnitude(uint8_t channel, unsigned magnitude_ua, uint8_t is_positive)
{
  uint16_t reg34;
  HAL_StatusTypeDef st;

  if (channel > 15U)
  {
    return HAL_ERROR;
  }
  if (magnitude_ua > 255U)
  {
    magnitude_ua = 255U;
  }

  st = Intan_ReadReg(34U, &reg34);
  if (st != HAL_OK)
  {
    return st;
  }
  if (reg34 != 0x00E2U)
  {
    st = Intan_WriteReg(34U, 0x00E2U, 0U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
    st = Intan_WriteReg(35U, 0x00AAU, 0U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
  }

  {
    uint16_t value = (magnitude_ua == 0U) ? 0x8000U : (uint16_t)(0x8000U | (magnitude_ua & 0xFFU));
    uint8_t reg_addr = (uint8_t)((is_positive ? 96U : 64U) + channel);
    return Intan_WriteReg(reg_addr, value, 0U, 0U);
  }
}

HAL_StatusTypeDef Intan_App_StimSetupCurrents(uint16_t ch_mask, unsigned neg_ua, unsigned pos_ua)
{
  HAL_StatusTypeDef st;
  unsigned i;

  if (neg_ua > 255U)
  {
    neg_ua = 255U;
  }
  if (pos_ua > 255U)
  {
    pos_ua = 255U;
  }

  for (i = 0U; i < 16U; i++)
  {
    if ((ch_mask & (1U << i)) == 0U)
    {
      continue;
    }
    st = Intan_App_SetStimMagnitude((uint8_t)i, neg_ua, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
    st = Intan_App_SetStimMagnitude((uint8_t)i, pos_ua, 1U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_StimEnable(uint16_t ch_mask, uint8_t enable, uint8_t negative_polarity)
{
  HAL_StatusTypeDef st;
  uint16_t polarity_mask = 0U;
  uint16_t stim_mask;
  uint8_t i;

  if (enable)
  {
    for (i = 0U; i < 16U; i++)
    {
      if ((ch_mask & (1U << i)) == 0U)
      {
        continue;
      }
      if (negative_polarity == 0U)
      {
        polarity_mask |= (uint16_t)(1U << i);
      }
    }
    st = Intan_WriteReg(44U, polarity_mask, 0U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();

    stim_mask = ch_mask;
    st = Intan_WriteReg(42U, stim_mask, 1U, 0U);
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
  }
  else
  {
    if (ch_mask == 0U)
    {
      st = Intan_WriteReg(42U, 0x0000U, 1U, 0U);
    }
    else
    {
      uint16_t cur42;
      st = Intan_ReadReg(42U, &cur42);
      if (st != HAL_OK)
      {
        return st;
      }
      for (i = 0U; i < 16U; i++)
      {
        if ((ch_mask & (1U << i)) != 0U)
        {
          cur42 &= (uint16_t) ~(1U << i);
        }
      }
      st = Intan_WriteReg(42U, cur42, 1U, 0U);
    }
    if (st != HAL_OK)
    {
      return st;
    }
    delay_post();
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_StimSawtooth(uint16_t ch_mask, unsigned steps, unsigned max_ua, uint32_t period_ms,
                                         uint32_t cycles)
{
  HAL_StatusTypeDef st;
  uint32_t c;
  unsigned s;
  unsigned cur;
  uint8_t i;
  uint32_t step_us;

  if (steps == 0U || max_ua > 255U || cycles == 0U)
  {
    return HAL_ERROR;
  }

  Intan_App_DWT_Reset();

  st = Intan_App_StimSetupCurrents(ch_mask, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  st = Intan_App_StimEnable(ch_mask, 1U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }

  step_us = (period_ms * 1000U) / (steps + 1U);
  if (step_us == 0U)
  {
    step_us = 1U;
  }

  for (c = 0U; c < cycles; c++)
  {
    for (s = 0U; s <= steps; s++)
    {
      cur = (max_ua * s) / steps;
      for (i = 0U; i < 16U; i++)
      {
        if ((ch_mask & (1U << i)) != 0U)
        {
          st = Intan_App_SetStimMagnitude(i, cur, 1U);
          if (st != HAL_OK)
          {
            (void)Intan_App_StimEnable(ch_mask, 0U, 0U);
            return st;
          }
        }
      }
      delay_us_busy(step_us);
    }
  }

  return Intan_App_StimEnable(ch_mask, 0U, 0U);
}

HAL_StatusTypeDef Intan_App_BenchConvert(uint32_t n, uint8_t channel, float *out_ksps_total, float *out_ksps_per_ch)
{
  uint32_t i;
  uint16_t v;
  uint32_t c0;
  uint32_t c1;
  float sec;

  if (n == 0U || out_ksps_total == NULL)
  {
    return HAL_ERROR;
  }

  Intan_App_DWT_Reset();
  c0 = DWT->CYCCNT;

  for (i = 0U; i < n; i++)
  {
    if (Intan_Convert(channel, 0U, &v) != HAL_OK)
    {
      return HAL_ERROR;
    }
  }

  c1 = DWT->CYCCNT;
  sec = (float)(c1 - c0) / (float)SystemCoreClock;
  if (sec <= 0.0f)
  {
    return HAL_ERROR;
  }

  *out_ksps_total = ((float)n / sec) / 1000.0f;
  if (out_ksps_per_ch != NULL)
  {
    if (channel == 63U)
    {
      *out_ksps_per_ch = (*out_ksps_total) / 16.0f;
    }
    else
    {
      *out_ksps_per_ch = *out_ksps_total;
    }
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_BenchConvertFast(uint32_t n, uint8_t channel, float *out_ksps_total,
                                             float *out_ksps_per_ch)
{
  uint16_t v;
  uint32_t c0;
  uint32_t c1;
  float sec;

  if (n == 0U || out_ksps_total == NULL)
  {
    return HAL_ERROR;
  }

  Intan_App_DWT_Reset();
  c0 = DWT->CYCCNT;

  if (Intan_ConvertPipeline(n, channel, 0U, &v) != HAL_OK)
  {
    return HAL_ERROR;
  }

  c1 = DWT->CYCCNT;
  sec = (float)(c1 - c0) / (float)SystemCoreClock;
  if (sec <= 0.0f)
  {
    return HAL_ERROR;
  }

  *out_ksps_total = ((float)n / sec) / 1000.0f;
  if (out_ksps_per_ch != NULL)
  {
    if (channel == 63U)
    {
      *out_ksps_per_ch = (*out_ksps_total) / 16.0f;
    }
    else
    {
      *out_ksps_per_ch = *out_ksps_total;
    }
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_BenchConvertTimCs(uint32_t n, uint8_t channel, uint32_t target_ksps,
                                              float *out_ksps_total, float *out_ksps_per_ch)
{
  uint16_t v;
  uint32_t c0;
  uint32_t c1;
  float sec;

  if (n == 0U || out_ksps_total == NULL)
  {
    return HAL_ERROR;
  }

  Intan_App_DWT_Reset();
  c0 = DWT->CYCCNT;

  if (Intan_ConvertPipelineTimCs(n, channel, 0U, target_ksps, &v) != HAL_OK)
  {
    return HAL_ERROR;
  }

  c1 = DWT->CYCCNT;
  sec = (float)(c1 - c0) / (float)SystemCoreClock;
  if (sec <= 0.0f)
  {
    return HAL_ERROR;
  }

  *out_ksps_total = ((float)n / sec) / 1000.0f;
  if (out_ksps_per_ch != NULL)
  {
    if (channel == 63U)
    {
      *out_ksps_per_ch = (*out_ksps_total) / 16.0f;
    }
    else
    {
      *out_ksps_per_ch = *out_ksps_total;
    }
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_App_BenchConvertDmaTimCs(uint32_t n, uint8_t channel, float *out_ksps_total,
                                                 float *out_ksps_per_ch)
{
  uint16_t v;
  uint32_t c0;
  uint32_t c1;
  float sec;

  if (n == 0U || out_ksps_total == NULL)
  {
    return HAL_ERROR;
  }

  Intan_App_DWT_Reset();
  c0 = DWT->CYCCNT;

  if (Intan_ConvertPipelineDmaTimCs(n, channel, 0U, &v) != HAL_OK)
  {
    return HAL_ERROR;
  }

  c1 = DWT->CYCCNT;
  sec = (float)(c1 - c0) / (float)SystemCoreClock;
  if (sec <= 0.0f)
  {
    return HAL_ERROR;
  }

  *out_ksps_total = ((float)n / sec) / 1000.0f;
  if (out_ksps_per_ch != NULL)
  {
    if (channel == 63U)
    {
      *out_ksps_per_ch = (*out_ksps_total) / 16.0f;
    }
    else
    {
      *out_ksps_per_ch = *out_ksps_total;
    }
  }
  return HAL_OK;
}

static int token_is_all(const char *tok)
{
  if (tok[0] == '*' && tok[1] == '\0')
  {
    return 1;
  }
  if ((tok[0] == 'A' || tok[0] == 'a') && (tok[1] == 'L' || tok[1] == 'l') && (tok[2] == 'L' || tok[2] == 'l') &&
      tok[3] == '\0')
  {
    return 1;
  }
  return 0;
}

int Intan_App_ParseChMask(const char *spec, uint16_t *out_mask)
{
  char buf[64];
  char *saveptr = NULL;
  char *tok;
  uint16_t m = 0U;

  if (spec == NULL || out_mask == NULL)
  {
    return -1;
  }

  while (*spec == ' ' || *spec == '\t')
  {
    spec++;
  }
  if (spec[0] == '\0')
  {
    return -1;
  }

  if (strlen(spec) >= sizeof(buf))
  {
    return -1;
  }
  memcpy(buf, spec, strlen(spec) + 1U);

  for (tok = strtok_r(buf, ",", &saveptr); tok != NULL; tok = strtok_r(NULL, ",", &saveptr))
  {
    while (*tok == ' ' || *tok == '\t')
    {
      tok++;
    }
    if (token_is_all(tok))
    {
      *out_mask = 0xFFFFU;
      return 0;
    }
    {
      char *dash = strchr(tok, '-');
      if (dash != NULL)
      {
        int a;
        int b;
        int k;
        *dash = '\0';
        a = (int)strtol(tok, NULL, 0);
        b = (int)strtol(dash + 1, NULL, 0);
        if (a < 0 || b > 15 || a > b)
        {
          return -1;
        }
        for (k = a; k <= b; k++)
        {
          m |= (uint16_t)(1U << (unsigned)k);
        }
      }
      else
      {
        int a = (int)strtol(tok, NULL, 0);
        if (a < 0 || a > 15)
        {
          return -1;
        }
        m |= (uint16_t)(1U << (unsigned)a);
      }
    }
  }

  *out_mask = m;
  if (m == 0U)
  {
    return -1;
  }
  return 0;
}
