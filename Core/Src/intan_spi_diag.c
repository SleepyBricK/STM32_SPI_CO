#include "intan_spi_diag.h"
#include "main.h"
#include "intan_spi.h"

static IntanSpiDiagSnapshot s_snap;
static uint8_t s_dwt_on;
static uint64_t s_period_acc;
static uint32_t s_period_samples;

static uint32_t intan_spi_prescaler_div(uint32_t mbr)
{
  /* SPI_CFG1 MBR: 000=/2, 001=/4, 010=/8, ... */
  return 2U << mbr;
}

void Intan_SpiDiag_Init(void)
{
  if (s_dwt_on == 0U)
  {
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CYCCNT = 0U;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    s_dwt_on = 1U;
  }
}

void Intan_SpiDiag_ReadClockConfig(IntanSpiDiagSnapshot *out)
{
  uint32_t pclk2;
  uint32_t mbr;

  if (out == NULL)
  {
    return;
  }

  Intan_SpiDiag_Init();

  out->spi_kernel_hz = HAL_RCCEx_GetPeriphCLKFreq(RCC_PERIPHCLK_SPI123);
  mbr = (READ_REG(SPI2->CFG1) & SPI_CFG1_MBR) >> SPI_CFG1_MBR_Pos;
  out->spi_prescaler_div = intan_spi_prescaler_div(mbr);
  if (out->spi_prescaler_div == 0U)
  {
    out->spi_prescaler_div = 1U;
  }
  out->spi_sck_hz_calc = out->spi_kernel_hz / out->spi_prescaler_div;

  pclk2 = HAL_RCC_GetPCLK2Freq();
  out->tim1_hz = ((RCC->D2CFGR & RCC_D2CFGR_D2PPRE2) == RCC_D2CFGR_D2PPRE2_DIV1) ? pclk2 : (pclk2 * 2U);
  out->tim_period_ticks = s_snap.tim_period_ticks;
  out->sample_period_avg_cycles = s_snap.sample_period_avg_cycles;
  out->block_cycles_last = s_snap.block_cycles_last;
  out->block_samples_last = s_snap.block_samples_last;
  out->block_slots_last = s_snap.block_slots_last;
  out->wall_cyc_per_sample = s_snap.wall_cyc_per_sample;
  out->wall_cycles_total = s_snap.wall_cycles_total;
  out->wall_samples_last = s_snap.wall_samples_last;
}

void Intan_SpiDiag_ResetTiming(void)
{
  s_period_acc = 0U;
  s_period_samples = 0U;
  s_snap.sample_period_avg_cycles = 0U;
  s_snap.block_cycles_last = 0U;
  s_snap.block_samples_last = 0U;
  s_snap.block_slots_last = 0U;
  s_snap.wall_cyc_per_sample = 0U;
  s_snap.wall_cycles_total = 0U;
  s_snap.wall_samples_last = 0U;
}

static uint32_t intan_spi_diag_cycles_delta(uint32_t cyc_start, uint32_t cyc_end)
{
  if (cyc_end >= cyc_start)
  {
    return cyc_end - cyc_start;
  }

  return (0xFFFFFFFFU - cyc_start) + cyc_end + 1U;
}

void Intan_SpiDiag_RecordWall(uint32_t cyc_start, uint32_t cyc_end, uint32_t samples)
{
  uint32_t total;

  if (samples == 0U)
  {
    return;
  }

  total = intan_spi_diag_cycles_delta(cyc_start, cyc_end);
  s_snap.wall_cycles_total = total;
  s_snap.wall_samples_last = samples;
  s_snap.wall_cyc_per_sample = total / samples;
}

uint32_t Intan_SpiDiag_KspsFromCycX10(uint32_t cyc_per_sample)
{
  uint64_t denom;

  if (cyc_per_sample == 0U)
  {
    return 0U;
  }

  /* Return kSamples/s * 10: SystemCoreClock / cycles gives samples/s. */
  denom = (uint64_t)cyc_per_sample * 1000ULL;
  return (uint32_t)(((uint64_t)SystemCoreClock * 10ULL) / denom);
}

void Intan_SpiDiag_RecordBlock(uint32_t cyc_start, uint32_t cyc_end, uint32_t samples,
                               uint32_t slots, uint32_t tim_period_ticks)
{
  uint32_t block_cyc;

  if (samples == 0U)
  {
    return;
  }

  if (cyc_end >= cyc_start)
  {
    block_cyc = cyc_end - cyc_start;
  }
  else
  {
    block_cyc = (0xFFFFFFFFU - cyc_start) + cyc_end + 1U;
  }

  s_snap.tim_period_ticks = tim_period_ticks;
  s_snap.block_cycles_last = block_cyc;
  s_snap.block_samples_last = samples;
  s_snap.block_slots_last = slots;

  s_period_acc += block_cyc;
  s_period_samples += samples;
  s_snap.sample_period_avg_cycles = (uint32_t)(s_period_acc / (uint64_t)s_period_samples);
}

const IntanSpiDiagSnapshot *Intan_SpiDiag_GetSnapshot(void)
{
  return &s_snap;
}
