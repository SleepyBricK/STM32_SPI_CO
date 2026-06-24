/**
 * @file intan_spi.c
 * @brief Порт linux intan_spi.c: SPI2, 32-битный кадр, CS между xfer; SPI без HAL_SPI_* см. intan_spi4_hw.c.
 */

#include "intan_spi.h"
#include "intan_spi4_hw.h"
#include "intan_spi_diag.h"
#include <stdint.h>
#include <string.h>

static uint8_t g_intan_spi_ready;
static Intan_IdleHookFn s_idle_hook;
static void *s_idle_ctx;
static volatile uint32_t g_spi_xfer32_count;
static volatile uint32_t g_sample_clip_count;

void Intan_SpiStats_Reset(void)
{
  g_spi_xfer32_count = 0U;
  g_sample_clip_count = 0U;
}

uint32_t Intan_GetSampleClipCount(void)
{
  return g_sample_clip_count;
}

void Intan_BumpSampleClip(void)
{
  g_sample_clip_count++;
}

static void intan_sample_clip_bump(void)
{
  Intan_BumpSampleClip();
}

uint32_t Intan_SpiStats_GetXfer32Count(void)
{
  return g_spi_xfer32_count;
}

void Intan_SpiStats_AddXfer32(uint32_t count)
{
  g_spi_xfer32_count += count;
}

void Intan_SetIdleHook(Intan_IdleHookFn fn, void *ctx)
{
  s_idle_hook = fn;
  s_idle_ctx = ctx;
}

#define INTAN_SPI_INSTANCE SPI2
#define INTAN_DMA_SUBCHUNK_MAX  (INTAN_DMA_CHUNK_SLOTS - INTAN_CONVERT_PIPELINE_LATENCY)
#define INTAN_CHUNK_RECOVER_SAMPLES  20U  /* legacy; recover removed — см. intan_spi.c */
#define INTAN_DMA_TIMCS_PERIOD_SCK_CYCLES 35U
#define INTAN_DMA_TIMCS_HIGH_NS 100U
#define INTAN_DMA_TIMCS_SPI_MIDI SPI_MASTER_INTERDATA_IDLENESS_03CYCLE
/* 32 SCK/frame + CS↑≥tCSOFF между слотами; 42 → ~595 kS/s slot. */
#define INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES 42U
#define INTAN_DMA_TIMSLOT_HIGH_NS 300U
#if INTAN_CS_HW_NSS
/* HW NSS: MIDI=4 SCK @ 25 MHz → 160 ns CS high (≥154 ns Intan); runtime: NSS_MIDI / Intan_SetStreamMidiCycles(). */
#define INTAN_DMA_STREAM_TX_REQUEST  DMA_REQUEST_SPI2_TX
#define INTAN_DMA_STREAM_SPI_MIDI_DEFAULT  4U
#else
#define INTAN_DMA_STREAM_TX_REQUEST  DMA_REQUEST_TIM1_UP
#define INTAN_DMA_STREAM_SPI_MIDI_DEFAULT  3U
#endif

static uint8_t s_stream_midi_cycles = INTAN_DMA_STREAM_SPI_MIDI_DEFAULT;
static uint32_t s_spi_prescaler_div = 8U;

void Intan_SetStreamMidiCycles(uint8_t cycles)
{
  if (cycles > 15U)
  {
    cycles = 15U;
  }
  s_stream_midi_cycles = cycles;
}

uint8_t Intan_GetStreamMidiCycles(void)
{
  return s_stream_midi_cycles;
}

uint32_t Intan_StreamMidiHal(void)
{
  return ((uint32_t)s_stream_midi_cycles << 4);
}

uint32_t Intan_GetSpiPrescalerDiv(void)
{
  return s_spi_prescaler_div;
}

#define INTAN_TIMSLOT_COLD_GAP_CYC  240000U
#define INTAN_TIMSLOT_SUBBLOCK_MAX  1024U
#define INTAN_CONVERT_PIPELINE_SLOTS  (2U * INTAN_CONVERT_PIPELINE_LATENCY)
#define INTAN_POLL_PIPE_DELAY_NOPS 16U
#define INTAN_IMPEDANCE_CS_OFF_NS  100U
#define INTAN_IMPEDANCE_CS_SETUP_NS 20U

static uint8_t s_dma_stream_continuous;
static uint8_t s_dma_stream_channel_count;
static uint8_t s_convert_pipeline_primed;
static uint32_t s_last_unpack_rx_offset;
static uint8_t s_dma_timslot_armed;
static uint8_t s_dma_timcs_armed;
static uint32_t s_dma_saved_midi;
static uint32_t s_timslot_last_end_cyc;
static volatile uint8_t s_timslot_burst_active;

uint8_t Intan_TimslotBurstIsActive(void)
{
  return s_timslot_burst_active;
}

static uint16_t s_stream_tail_adc = 0x8000U;
static uint16_t s_stream_tail_adc_ch[16];
static uint8_t s_unpack_first_ch;
static uint8_t s_unpack_n_ch;
static uint8_t s_unpack_phase;

static uint32_t s_dma_tx_word __attribute__((section(".dma_buffer"), aligned(32)));
static uint32_t s_dma_tx_words[INTAN_DMA_CHUNK_SLOTS] __attribute__((section(".dma_buffer"), aligned(32)));
static uint32_t s_dma_rx_words[INTAN_DMA_CHUNK_SLOTS] __attribute__((section(".dma_buffer"), aligned(32)));

/* RHS2116: tCSOFF -- CS high минимум 100 нс между 32-битными циклами. */
#if INTAN_CS_HW_NSS
#define INTAN_LEGACY_UNUSED  __attribute__((unused))
#else
#define INTAN_LEGACY_UNUSED
#endif

static void INTAN_LEGACY_UNUSED intan_delay_ns(uint32_t ns)
{
  uint32_t cycles =
      (uint32_t)(((uint64_t)SystemCoreClock * (uint64_t)ns + 999999999ULL) / 1000000000ULL);
  if (cycles < 6U)
  {
    cycles = 6U;
  }
  volatile uint32_t c = cycles;
  while (c-- != 0U)
  {
    __NOP();
  }
}

static inline uint32_t intan_pack_be4(uint8_t b0, uint8_t b1, uint8_t b2, uint8_t b3)
{
  return ((uint32_t)b0 << 24) | ((uint32_t)b1 << 16) | ((uint32_t)b2 << 8) | (uint32_t)b3;
}

static uint32_t intan_convert_cmd_word(uint8_t channel, uint8_t flags);

static inline uint32_t intan_pack_be_buf(const uint8_t *cmd4)
{
  return intan_pack_be4(cmd4[0], cmd4[1], cmd4[2], cmd4[3]);
}

/*
 * READ: reg_value = (resp[2] << 8) | resp[3] — младшие 16 бит 32-bit кадра.
 * CONVERT: ADC в старших 16 битах w — (w >> 16) & 0xFFFF.
 */
static inline uint16_t intan_u16_from_read_word(uint32_t w)
{
  return (uint16_t)(w & 0xFFFFU);
}

static inline uint16_t intan_u16_from_convert_word(uint32_t w)
{
  return (uint16_t)((w >> 16) & 0xFFFFU);
}

/** Intan framework / datasheet: CONVERT AC sample = upper 16 bits of 32-bit MISO word. */
static uint16_t intan_adc_from_convert_rx(uint32_t w, uint16_t prev)
{
  (void)prev;
  return intan_u16_from_convert_word(w);
}

static inline uint8_t intan_rr_abs_channel(uint8_t first_ch, uint8_t n_ch, uint8_t phase, uint32_t i)
{
  return (uint8_t)(first_ch + ((phase + i) % (uint32_t)n_ch));
}

static void intan_unpack_set_rr_context(uint8_t first_ch, uint8_t n_ch, uint8_t phase)
{
  s_unpack_first_ch = first_ch;
  s_unpack_n_ch = n_ch;
  s_unpack_phase = phase;
}

static void intan_unpack_rr_sanitize_block(uint16_t *samples, uint32_t n, uint32_t rx_offset)
{
  uint8_t first_ch = s_unpack_first_ch;
  uint8_t n_ch = s_unpack_n_ch;
  uint8_t phase = s_unpack_phase;
  uint32_t i;

  for (i = 0U; i < n; i++)
  {
    uint8_t ch = intan_rr_abs_channel(first_ch, n_ch, phase, i);
    uint16_t prev = s_stream_tail_adc_ch[ch];
    uint16_t adc = intan_adc_from_convert_rx(s_dma_rx_words[i + rx_offset], prev);

    samples[i] = adc;
    s_stream_tail_adc_ch[ch] = adc;
  }
}

static void intan_unpack_convert_block(uint16_t *samples, uint32_t n, uint32_t rx_offset, uint16_t prev_in)
{
  uint32_t i;
  uint16_t prev = prev_in;

  for (i = 0U; i < n; i++)
  {
    samples[i] = intan_adc_from_convert_rx(s_dma_rx_words[i + rx_offset], prev);
    prev = samples[i];
  }
}

static void INTAN_LEGACY_UNUSED intan_cs_low(void)
{
#if !INTAN_CS_HW_NSS
  INTAN_CS_GPIO_PORT->BSRR = ((uint32_t)INTAN_CS_PIN << 16);
#else
  (void)0;
#endif
}

static void INTAN_LEGACY_UNUSED intan_cs_high(void)
{
#if !INTAN_CS_HW_NSS
  INTAN_CS_GPIO_PORT->BSRR = (uint32_t)INTAN_CS_PIN;
#else
  (void)0;
#endif
}

static inline void intan_poll_pipe_delay(void)
{
  for (uint32_t i = 0U; i < INTAN_POLL_PIPE_DELAY_NOPS; i++)
  {
    __NOP();
  }
}

static HAL_StatusTypeDef intan_xfer32(uint32_t tx_word, uint32_t *rx_out)
{
  HAL_StatusTypeDef st;
  uint32_t rx_word = 0U;

  if (!g_intan_spi_ready)
  {
    return HAL_ERROR;
  }

  /*
   * RHS2116: один 32-бит кадр — Intan_SPI4_Transfer32 (TXDR/RXDR, без HAL_SPI_TransmitReceive).
   * INTAN_CS_HW_NSS: pulsed NSS на PA11; иначе программный CS PE11.
   */
#if !INTAN_CS_HW_NSS
  intan_delay_ns(500U);
  intan_cs_low();
  intan_delay_ns(120U);
#endif
  st = Intan_SPI4_Transfer32(INTAN_SPI_INSTANCE, tx_word, &rx_word, INTAN_SPI_TIMEOUT_MS);
#if !INTAN_CS_HW_NSS
  intan_cs_high();
#endif

  if (rx_out != NULL)
  {
    *rx_out = rx_word;
  }

  if (st == HAL_OK)
  {
    Intan_SpiStats_AddXfer32(1U);
  }
  return st;
}

/** Zcheck: один 32-битный RHS2116 кадр; CS↑ между каждым кадром (tCSOFF ≥100 ns). */
static HAL_StatusTypeDef intan_xfer32_fast(uint32_t tx_word, uint32_t *rx_out)
{
  HAL_StatusTypeDef st;
  uint32_t rx_word = 0U;

  if (!g_intan_spi_ready)
  {
    return HAL_ERROR;
  }

#if !INTAN_CS_HW_NSS
  intan_delay_ns(INTAN_IMPEDANCE_CS_OFF_NS);
  intan_cs_low();
  intan_delay_ns(INTAN_IMPEDANCE_CS_SETUP_NS);
#endif
  st = Intan_SPI4_Transfer32(INTAN_SPI_INSTANCE, tx_word, &rx_word, INTAN_SPI_TIMEOUT_MS);
#if !INTAN_CS_HW_NSS
  intan_cs_high();
#endif

  if (rx_out != NULL)
  {
    *rx_out = rx_word;
  }

  if (st == HAL_OK)
  {
    Intan_SpiStats_AddXfer32(1U);
  }
  return st;
}

static inline uint32_t intan_write_reg3_word(uint8_t dac_val)
{
  return intan_pack_be4(0x80U, 3U, 0x00U, dac_val);
}

static HAL_StatusTypeDef intan_convert_adc_fast(uint8_t channel, uint8_t flags, uint16_t *adc_out)
{
  HAL_StatusTypeDef st;
  uint32_t rx = 0U;
  uint32_t cmd = intan_convert_cmd_word(channel, flags);

  /* CONVERT: три отдельных CS-транзакции (cmd, 0, 0+ADC) — как Intan_Convert(). */
  st = intan_xfer32_fast(cmd, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32_fast(0U, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32_fast(0U, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (adc_out != NULL)
  {
    *adc_out = intan_u16_from_convert_word(rx);
  }
  return HAL_OK;
}

/** WRITE Reg3: один CS-кадр (данные latch на первом слове). */
static HAL_StatusTypeDef intan_write_reg3_fast(uint8_t dac_val)
{
  return intan_xfer32_fast(intan_write_reg3_word(dac_val), NULL);
}

static HAL_StatusTypeDef intan_xfer32_repeat_fast(uint32_t tx_word, uint32_t n, uint32_t *last_rx_out)
{
  const uint32_t max_slots_per_chunk = 30000U;
  uint32_t last_rx = 0U;
  __IO uint32_t *txdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->RXDR;
#if !INTAN_CS_HW_NSS
  const uint32_t cs_set = (uint32_t)INTAN_CS_PIN;
  const uint32_t cs_reset = ((uint32_t)INTAN_CS_PIN << 16);
#endif

  if (!g_intan_spi_ready || n == 0U)
  {
    return HAL_ERROR;
  }

  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);

  while (n != 0U)
  {
    uint32_t chunk_slots = (n > max_slots_per_chunk) ? max_slots_per_chunk : n;
    uint32_t i;

    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = chunk_slots;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    for (i = 0U; i < chunk_slots; i++)
    {
#if !INTAN_CS_HW_NSS
      INTAN_CS_GPIO_PORT->BSRR = cs_reset;
#endif

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_TXP) == 0U) {}
      *txdr32 = tx_word;

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_RXP) == 0U) {}
      last_rx = *rxdr32;
#if !INTAN_CS_HW_NSS
      INTAN_CS_GPIO_PORT->BSRR = cs_set;
#endif
    }

    while ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U) {}
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    Intan_SpiStats_AddXfer32(chunk_slots);
    n -= chunk_slots;
  }

  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

  if (last_rx_out != NULL)
  {
    *last_rx_out = last_rx;
  }
  return HAL_OK;
}

static void intan_cs_gpio_mode(void)
{
#if !INTAN_CS_HW_NSS
  GPIO_InitTypeDef gpio = {0};

  gpio.Pin = INTAN_CS_PIN;
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_PULLUP;
  gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  HAL_GPIO_Init(INTAN_CS_GPIO_PORT, &gpio);
  intan_cs_high();
#else
  (void)0;
#endif
}

static void intan_cs_tim1_ch2_mode(void)
{
#if !INTAN_CS_HW_NSS
  GPIO_InitTypeDef gpio = {0};

  __HAL_RCC_TIM1_CLK_ENABLE();
  __HAL_RCC_GPIOE_CLK_ENABLE();

  gpio.Pin = INTAN_CS_PIN;
  gpio.Mode = GPIO_MODE_AF_PP;
  gpio.Pull = GPIO_PULLUP;
  gpio.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  gpio.Alternate = GPIO_AF1_TIM1;
  HAL_GPIO_Init(INTAN_CS_GPIO_PORT, &gpio);
#else
  (void)0;
#endif
}

static uint32_t intan_tim1_clock_hz(void)
{
  uint32_t pclk = HAL_RCC_GetPCLK2Freq();
  return ((RCC->D2CFGR & RCC_D2CFGR_D2PPRE2) == RCC_D2CFGR_D2PPRE2_DIV1) ? pclk : (pclk * 2U);
}

static void intan_timcs_recover(void)
{
  TIM1->CR1 &= ~TIM_CR1_CEN;
  TIM1->CCER &= ~TIM_CCER_CC2E;
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  intan_cs_gpio_mode();
}

static HAL_StatusTypeDef intan_wait_reg_flag_guard(__IO uint32_t *reg, uint32_t flag)
{
  uint32_t guard = 10000000U;
  while ((*reg & flag) == 0U)
  {
    if (s_idle_hook != NULL && s_timslot_burst_active == 0U)
    {
      s_idle_hook(s_idle_ctx);
    }
    if (--guard == 0U)
    {
      return HAL_TIMEOUT;
    }
  }
  return HAL_OK;
}

/** SPI TIM-slot burst: без USB IRQ и без idle_hook (USB bulk DMA ломает CS↔SPI на ~3k слоте). */
static void intan_timslot_burst_enter(void)
{
  s_timslot_burst_active = 1U;
  NVIC_DisableIRQ(OTG_HS_IRQn);
  __DSB();
  __ISB();
}

static void intan_timslot_burst_exit(void)
{
  s_timslot_burst_active = 0U;
  NVIC_EnableIRQ(OTG_HS_IRQn);
}

static HAL_StatusTypeDef intan_wait_dma_stream_disabled(DMA_Stream_TypeDef *stream)
{
  uint32_t guard = 1000000U;
  while ((stream->CR & DMA_SxCR_EN) != 0U)
  {
    if (s_idle_hook != NULL)
    {
      s_idle_hook(s_idle_ctx);
    }
    if (--guard == 0U)
    {
      return HAL_TIMEOUT;
    }
  }
  return HAL_OK;
}

static void intan_dma_timcs_recover(uint32_t old_midi)
{
#if !INTAN_CS_HW_NSS
  TIM1->CR1 &= ~TIM_CR1_CEN;
  TIM1->CCER &= ~TIM_CCER_CC2E;
  TIM1->DIER = 0U;
#endif

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  (void)intan_wait_dma_stream_disabled(DMA1_Stream0);
  (void)intan_wait_dma_stream_disabled(DMA1_Stream1);

  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, old_midi);

#if !INTAN_CS_HW_NSS
  intan_cs_gpio_mode();
#endif
}

void Intan_SetDmaStreamContinuous(uint8_t enable)
{
  s_dma_stream_continuous = (enable != 0U) ? 1U : 0U;
  if (enable == 0U)
  {
    s_convert_pipeline_primed = 0U;
  }
}

void Intan_SetDmaStreamChannelCount(uint8_t channel_count)
{
  s_dma_stream_channel_count = (channel_count == 0U) ? 1U : channel_count;
}

void Intan_DmaPathRelease(void)
{
  uint32_t midi = READ_REG(INTAN_SPI_INSTANCE->CFG2) & SPI_CFG2_MIDI;

  s_dma_stream_continuous = 0U;
  s_dma_stream_channel_count = 1U;
  s_convert_pipeline_primed = 0U;
  s_dma_timslot_armed = 0U;
  s_dma_timcs_armed = 0U;
  s_timslot_last_end_cyc = 0U;
  s_stream_tail_adc = 0x8000U;
  for (uint32_t i = 0U; i < 16U; i++)
  {
    s_stream_tail_adc_ch[i] = 0x8000U;
  }
  intan_dma_timcs_recover(midi);
}

/*
 * RHS2116 CONVERT pipeline (Intan framework): MISO[k] = результат MOSI[k-2].
 *
 * Single-channel (repeat CONVERT(ch)):
 *   cold: n+2 TX slots, unpack n samples from RX[2..n+1]
 *   hot:  n+2 TX slots, unpack n samples from RX[0..n-1] (tail prev sub-block)
 *
 * RR/range (2x CONVERT(63) prime on cold):
 *   cold: 2 prime + n + 2 tail, unpack from RX[4..n+3]
 *   hot:  n + 2 tail, unpack from RX[0..n-1]
 */
static uint8_t intan_pipeline_is_hot(void)
{
  return (s_dma_stream_continuous != 0U && s_convert_pipeline_primed != 0U) ? 1U : 0U;
}

static void intan_pipeline_layout_poll(uint32_t n, uint32_t *chunk_slots, uint32_t *rx_offset)
{
  if (intan_pipeline_is_hot() != 0U)
  {
    *chunk_slots = n + INTAN_CONVERT_PIPELINE_LATENCY;
    *rx_offset = 0U;
  }
  else
  {
    *chunk_slots = n + (2U * INTAN_CONVERT_PIPELINE_LATENCY);
    *rx_offset = 2U * INTAN_CONVERT_PIPELINE_LATENCY;
  }
}

static void intan_pipeline_layout_timslot(uint32_t n, uint32_t *chunk_slots, uint32_t *rx_offset,
                                          uint8_t hot_chain)
{
  if (hot_chain != 0U)
  {
    *chunk_slots = n + INTAN_CONVERT_PIPELINE_LATENCY;
    *rx_offset = 0U;
  }
  else
  {
    *chunk_slots = n + (2U * INTAN_CONVERT_PIPELINE_LATENCY);
    *rx_offset = 2U * INTAN_CONVERT_PIPELINE_LATENCY;
  }
}

static uint8_t intan_timslot_gap_is_cold(void)
{
  if (s_timslot_last_end_cyc == 0U)
  {
    return 1U;
  }
  return ((DWT->CYCCNT - s_timslot_last_end_cyc) > INTAN_TIMSLOT_COLD_GAP_CYC) ? 1U : 0U;
}

static void intan_timslot_mark_burst_end(void)
{
  s_timslot_last_end_cyc = DWT->CYCCNT;
}

static void intan_pipeline_layout_single(uint32_t n, uint32_t *chunk_slots, uint32_t *rx_offset,
                                         uint8_t hot_chain);

static void intan_pipeline_layout_remember_poll(uint32_t n, uint32_t *chunk_slots,
                                                uint32_t *rx_offset)
{
  intan_pipeline_layout_poll(n, chunk_slots, rx_offset);
  s_last_unpack_rx_offset = *rx_offset;
}

static void intan_pipeline_layout_remember_single(uint32_t n, uint32_t *chunk_slots,
                                                  uint32_t *rx_offset, uint8_t hot_chain)
{
  intan_pipeline_layout_single(n, chunk_slots, rx_offset, hot_chain);
  s_last_unpack_rx_offset = *rx_offset;
}

static void intan_pipeline_layout_remember_timslot(uint32_t n, uint32_t *chunk_slots,
                                                   uint32_t *rx_offset, uint8_t continuing_sub)
{
  uint8_t hot_chain =
      (continuing_sub != 0U && intan_timslot_gap_is_cold() == 0U) ? 1U : 0U;
  intan_pipeline_layout_timslot(n, chunk_slots, rx_offset, hot_chain);
  s_last_unpack_rx_offset = *rx_offset;
}

uint32_t Intan_GetLastUnpackRxOffset(void)
{
  return s_last_unpack_rx_offset;
}

uint8_t Intan_PipelineChannelIndex(uint8_t phase, uint32_t sample_index, uint32_t rx_offset,
                                   uint8_t n_ch)
{
  uint32_t ch_idx;

  if (n_ch == 0U)
  {
    return 0U;
  }

  ch_idx = (uint32_t)phase + sample_index + rx_offset;
  if (rx_offset >= INTAN_CONVERT_PIPELINE_LATENCY)
  {
    ch_idx -= INTAN_CONVERT_PIPELINE_LATENCY;
  }
  ch_idx += (uint32_t)n_ch * 4U;
  ch_idx %= (uint32_t)n_ch;
  return (uint8_t)ch_idx;
}

static void intan_pipeline_mark_done(void)
{
  if (s_dma_stream_continuous != 0U)
  {
    s_convert_pipeline_primed = 1U;
  }
}

static HAL_StatusTypeDef intan_pipeline_validate_n(uint32_t n, uint32_t chunk_slots)
{
  if (n == 0U || chunk_slots > INTAN_DMA_CHUNK_SLOTS)
  {
    return HAL_ERROR;
  }
  return HAL_OK;
}

/** Между USB-chunk: SPI/DMA стоп; legacy — TIM1 CH2 на PE11. */
static void intan_dma_chunk_gap_reset(void)
{
#if INTAN_CS_HW_NSS
  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  (void)intan_wait_dma_stream_disabled(DMA1_Stream0);
  (void)intan_wait_dma_stream_disabled(DMA1_Stream1);

  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
#else
  TIM1->CR1 &= ~TIM_CR1_CEN;
  TIM1->DIER = 0U;

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  (void)intan_wait_dma_stream_disabled(DMA1_Stream0);
  (void)intan_wait_dma_stream_disabled(DMA1_Stream1);

  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
#endif
}

static void intan_timslot_chunk_gap_reset(void)
{
  intan_dma_chunk_gap_reset();
}

static void intan_dma_block_halt(void)
{
#if !INTAN_CS_HW_NSS
  TIM1->CR1 &= ~TIM_CR1_CEN;
  TIM1->DIER = 0U;
  TIM1->CCER &= ~TIM_CCER_CC2E;
#endif

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  (void)intan_wait_dma_stream_disabled(DMA1_Stream0);
  (void)intan_wait_dma_stream_disabled(DMA1_Stream1);

  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
#if !INTAN_CS_HW_NSS
  intan_cs_gpio_mode();
#endif
}

static void INTAN_LEGACY_UNUSED intan_timslot_tim_kick(uint8_t fresh_start)
{
  if (fresh_start != 0U)
  {
    TIM1->CNT = 0U;
    TIM1->SR = 0U;
  }
  TIM1->CCER |= TIM_CCER_CC2E;
  TIM1->DIER = TIM_DIER_UDE;
  TIM1->CR1 |= TIM_CR1_CEN;
}

static void intan_timslot_cs_prepare_burst(uint8_t hot_chain)
{
#if INTAN_CS_HW_NSS
  (void)hot_chain;
#else
  if (hot_chain == 0U)
  {
    intan_cs_gpio_mode();
    intan_delay_ns(500U);
  }
  intan_cs_tim1_ch2_mode();
#endif
}

static void intan_dma_finish_read(uint32_t old_midi, uint8_t tim_path_armed, uint8_t halt_after)
{
  intan_pipeline_mark_done();
  if (s_dma_stream_continuous != 0U && tim_path_armed != 0U)
  {
    if (halt_after != 0U)
    {
      intan_dma_block_halt();
    }
    else
    {
      intan_timslot_chunk_gap_reset();
    }
    return;
  }

  intan_dma_timcs_recover(old_midi);
  s_dma_timslot_armed = 0U;
  s_dma_timcs_armed = 0U;
  if (s_dma_stream_continuous == 0U)
  {
    s_convert_pipeline_primed = 0U;
  }
}

static HAL_StatusTypeDef intan_dma_compute_tim_ticks(uint32_t period_sck_cycles, uint32_t cs_high_ns,
                                                   uint32_t *period_ticks, uint32_t *low_ticks)
{
  uint32_t tim_clk = intan_tim1_clock_hz();
  uint32_t spi_sck_hz = 25000000U;
  uint32_t high_ticks;

  *period_ticks = (uint32_t)(((uint64_t)tim_clk * period_sck_cycles + (spi_sck_hz / 2U)) / spi_sck_hz);
  high_ticks = (uint32_t)(((uint64_t)tim_clk * cs_high_ns + 999999999ULL) / 1000000000ULL);
  if (high_ticks < 2U)
  {
    high_ticks = 2U;
  }
  if (*period_ticks <= high_ticks + 4U)
  {
    return HAL_ERROR;
  }
  *low_ticks = *period_ticks - high_ticks;
  return HAL_OK;
}

/** TIM1 slot period for paced Zcheck: slot_hz SPI frames/s, CS↑ on каждый slot (TIM1 CH2). */
static HAL_StatusTypeDef intan_dma_compute_tim_ticks_from_slot_hz(uint32_t slot_hz,
                                                                  uint32_t *period_ticks,
                                                                  uint32_t *low_ticks)
{
  uint32_t tim_clk = intan_tim1_clock_hz();
  uint32_t high_ticks;

  if (slot_hz == 0U || tim_clk < slot_hz)
  {
    return HAL_ERROR;
  }

  *period_ticks = (uint32_t)(((uint64_t)tim_clk + (slot_hz / 2U)) / slot_hz);
  high_ticks = (uint32_t)(((uint64_t)tim_clk * INTAN_DMA_TIMSLOT_HIGH_NS + 999999999ULL) /
                          1000000000ULL);
  if (high_ticks < 2U)
  {
    high_ticks = 2U;
  }
  if (*period_ticks <= high_ticks + 4U)
  {
    return HAL_ERROR;
  }
  *low_ticks = *period_ticks - high_ticks;
  return HAL_OK;
}

static HAL_StatusTypeDef intan_dma_timslot_ensure_armed(uint32_t period_ticks, uint32_t low_ticks,
                                                        uint32_t *old_midi_out)
{
  (void)period_ticks;
  (void)low_ticks;

  if (s_dma_timslot_armed != 0U)
  {
    *old_midi_out = s_dma_saved_midi;
    return HAL_OK;
  }

  __HAL_RCC_DMA1_CLK_ENABLE();
#if !INTAN_CS_HW_NSS
  intan_cs_tim1_ch2_mode();

  TIM1->CR1 = 0U;
  TIM1->CR2 = 0U;
  TIM1->SMCR = 0U;
  TIM1->DIER = 0U;
  TIM1->PSC = 0U;
  TIM1->ARR = period_ticks - 1U;
  TIM1->CCR2 = low_ticks;
  TIM1->CCMR1 &= ~(TIM_CCMR1_OC2M | TIM_CCMR1_CC2S);
  TIM1->CCMR1 |= (6U << TIM_CCMR1_OC2M_Pos) | TIM_CCMR1_OC2PE;
  TIM1->CCER &= ~TIM_CCER_CC2E;
  TIM1->CCER |= TIM_CCER_CC2P;
  TIM1->BDTR |= TIM_BDTR_MOE;
  TIM1->EGR = TIM_EGR_UG;
  TIM1->SR = 0U;
#endif

  s_dma_saved_midi = INTAN_SPI_INSTANCE->CFG2 & SPI_CFG2_MIDI;
  s_dma_timslot_armed = 1U;
  *old_midi_out = s_dma_saved_midi;
  return HAL_OK;
}

static HAL_StatusTypeDef intan_dma_timcs_ensure_armed(uint32_t period_ticks, uint32_t low_ticks,
                                                      uint32_t *old_midi_out)
{
  (void)period_ticks;
  (void)low_ticks;

  if (s_dma_timcs_armed != 0U)
  {
    *old_midi_out = s_dma_saved_midi;
    return HAL_OK;
  }

  __HAL_RCC_DMA1_CLK_ENABLE();
#if !INTAN_CS_HW_NSS
  intan_cs_tim1_ch2_mode();

  TIM1->CR1 = 0U;
  TIM1->CR2 = 0U;
  TIM1->SMCR = 0U;
  TIM1->DIER = 0U;
  TIM1->PSC = 0U;
  TIM1->ARR = period_ticks - 1U;
  TIM1->CCR2 = low_ticks;
  TIM1->CCMR1 &= ~(TIM_CCMR1_OC2M | TIM_CCMR1_CC2S);
  TIM1->CCMR1 |= (6U << TIM_CCMR1_OC2M_Pos) | TIM_CCMR1_OC2PE;
  TIM1->CCER &= ~TIM_CCER_CC2E;
  TIM1->CCER |= TIM_CCER_CC2P;
  TIM1->BDTR |= TIM_BDTR_MOE;
  TIM1->EGR = TIM_EGR_UG;
  TIM1->SR = 0U;
#endif

  s_dma_saved_midi = INTAN_SPI_INSTANCE->CFG2 & SPI_CFG2_MIDI;
  s_dma_timcs_armed = 1U;
  *old_midi_out = s_dma_saved_midi;
  return HAL_OK;
}

static HAL_StatusTypeDef intan_dma_prepare_streams_ex(const uint32_t *tx_ptr, uint32_t chunk_slots,
                                                      uint8_t tx_minc, uint32_t tx_request)
{
  const uint32_t dma_stream0_flags = DMA_LIFCR_CFEIF0 | DMA_LIFCR_CDMEIF0 | DMA_LIFCR_CTEIF0 |
                                     DMA_LIFCR_CHTIF0 | DMA_LIFCR_CTCIF0;
  const uint32_t dma_stream1_flags = DMA_LIFCR_CFEIF1 | DMA_LIFCR_CDMEIF1 | DMA_LIFCR_CTEIF1 |
                                     DMA_LIFCR_CHTIF1 | DMA_LIFCR_CTCIF1;
  uint32_t tx_cr;

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  if (intan_wait_dma_stream_disabled(DMA1_Stream0) != HAL_OK ||
      intan_wait_dma_stream_disabled(DMA1_Stream1) != HAL_OK)
  {
    return HAL_TIMEOUT;
  }

  DMA1->LIFCR = dma_stream0_flags | dma_stream1_flags;
  DMAMUX1_Channel0->CCR = DMA_REQUEST_SPI2_RX;
  DMAMUX1_Channel1->CCR = tx_request;

  DMA1_Stream0->PAR = (uint32_t)&INTAN_SPI_INSTANCE->RXDR;
  DMA1_Stream0->M0AR = (uint32_t)s_dma_rx_words;
  DMA1_Stream0->NDTR = chunk_slots;
  DMA1_Stream0->FCR = 0U;
  DMA1_Stream0->CR = DMA_SxCR_PL_1 | DMA_SxCR_MSIZE_1 | DMA_SxCR_PSIZE_1 | DMA_SxCR_MINC;

  tx_cr = DMA_SxCR_PL_1 | DMA_SxCR_DIR_0 | DMA_SxCR_MSIZE_1 | DMA_SxCR_PSIZE_1;
  if (tx_minc != 0U)
  {
    tx_cr |= DMA_SxCR_MINC;
  }

  DMA1_Stream1->PAR = (uint32_t)&INTAN_SPI_INSTANCE->TXDR;
  DMA1_Stream1->M0AR = (uint32_t)tx_ptr;
  DMA1_Stream1->NDTR = chunk_slots;
  DMA1_Stream1->FCR = 0U;
  DMA1_Stream1->CR = tx_cr;

  return HAL_OK;
}

static HAL_StatusTypeDef intan_dma_prepare_streams(uint32_t tx_word, uint32_t chunk_slots)
{
  s_dma_tx_word = tx_word;
  return intan_dma_prepare_streams_ex(&s_dma_tx_word, chunk_slots, 0U, DMA_REQUEST_SPI2_TX);
}

static uint32_t intan_convert_cmd_word(uint8_t channel, uint8_t flags)
{
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);

  return intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                        0x00U, 0x00U);
}

static void intan_pipeline_layout_single(uint32_t n, uint32_t *chunk_slots, uint32_t *rx_offset,
                                         uint8_t hot_chain)
{
  *chunk_slots = n + INTAN_CONVERT_PIPELINE_LATENCY;
  *rx_offset = (hot_chain != 0U) ? 0U : INTAN_CONVERT_PIPELINE_LATENCY;
}

static void intan_fill_single_tx_words(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t slots = n + INTAN_CONVERT_PIPELINE_LATENCY;
  uint32_t cmd = intan_convert_cmd_word(channel, flags);
  uint32_t i;

  for (i = 0U; i < slots; i++)
  {
    s_dma_tx_words[i] = cmd;
  }
}

static void intan_fill_rr_range_tx_words(uint32_t n, uint8_t first_ch, uint8_t n_ch, uint8_t flags,
                                         uint8_t phase, uint8_t pipeline_cold)
{
  uint32_t slot = 0U;
  uint32_t i;

  if (pipeline_cold != 0U)
  {
    for (i = 0U; i < INTAN_CONVERT_PIPELINE_LATENCY; i++)
    {
      s_dma_tx_words[slot++] = intan_convert_cmd_word(63U, flags);
    }
  }

  for (i = 0U; i < n; i++)
  {
    s_dma_tx_words[slot++] =
        intan_convert_cmd_word((uint8_t)(first_ch + ((phase + i) % n_ch)), flags);
  }

  for (i = 0U; i < INTAN_CONVERT_PIPELINE_LATENCY; i++)
  {
    uint8_t tail_ch = (pipeline_cold != 0U) ? 63U :
                      (uint8_t)(first_ch + ((phase + n + i) % n_ch));

    s_dma_tx_words[slot++] = intan_convert_cmd_word(tail_ch, flags);
  }
}

static void intan_fill_rr_tx_words(uint32_t n, uint8_t n_ch, uint8_t flags, uint8_t phase,
                                   uint8_t pipeline_cold)
{
  intan_fill_rr_range_tx_words(n, 0U, n_ch, flags, phase, pipeline_cold);
}

static HAL_StatusTypeDef intan_xfer32_repeat_dma_timcs(uint32_t tx_word, uint32_t n, uint32_t *last_rx_out)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
  uint32_t tim_clk;
  uint32_t spi_sck_hz;
  uint32_t period_ticks;
  uint32_t high_ticks;
  uint32_t low_ticks;
  uint32_t old_midi;
  uint32_t last_rx = 0U;

  if (!g_intan_spi_ready || n == 0U)
  {
    return HAL_ERROR;
  }

  __HAL_RCC_DMA1_CLK_ENABLE();
  intan_cs_tim1_ch2_mode();

  /*
   * TIM1 поднимает CS на idle-окне между 32-битными DMA-словами.
   * Фактический поток на плате зависит от SYSCLK и обычно получается ниже
   * теоретического лимита таймера/SCK.
   */
  tim_clk = intan_tim1_clock_hz();
  spi_sck_hz = 25000000U;
  period_ticks = (uint32_t)(((uint64_t)tim_clk * INTAN_DMA_TIMCS_PERIOD_SCK_CYCLES +
                             (spi_sck_hz / 2U)) / spi_sck_hz);
  high_ticks = (uint32_t)(((uint64_t)tim_clk * INTAN_DMA_TIMCS_HIGH_NS + 999999999ULL) / 1000000000ULL);
  if (high_ticks < 2U)
  {
    high_ticks = 2U;
  }
  if (period_ticks <= high_ticks + 4U)
  {
    intan_cs_gpio_mode();
    return HAL_ERROR;
  }
  low_ticks = period_ticks - high_ticks;

  TIM1->CR1 = 0U;
  TIM1->CR2 = 0U;
  TIM1->SMCR = 0U;
  TIM1->DIER = 0U;
  TIM1->PSC = 0U;
  TIM1->ARR = period_ticks - 1U;
  TIM1->CCR2 = low_ticks;
  TIM1->CCMR1 &= ~(TIM_CCMR1_OC2M | TIM_CCMR1_CC2S);
  TIM1->CCMR1 |= (6U << TIM_CCMR1_OC2M_Pos) | TIM_CCMR1_OC2PE; /* PWM1. */
  TIM1->CCER &= ~TIM_CCER_CC2E;
  TIM1->CCER |= TIM_CCER_CC2P; /* Active low while CNT < CCR2. */
  TIM1->BDTR |= TIM_BDTR_MOE;
  TIM1->EGR = TIM_EGR_UG;
  TIM1->SR = 0U;

  old_midi = INTAN_SPI_INSTANCE->CFG2 & SPI_CFG2_MIDI;

  while (n != 0U)
  {
    uint32_t chunk_slots = (n > INTAN_DMA_CHUNK_SLOTS) ? INTAN_DMA_CHUNK_SLOTS : n;

    if (intan_dma_prepare_streams(tx_word, chunk_slots) != HAL_OK)
    {
      intan_dma_timcs_recover(old_midi);
      return HAL_TIMEOUT;
    }

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, INTAN_DMA_TIMCS_SPI_MIDI);
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = chunk_slots;
    INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;

    DMA1_Stream0->CR |= DMA_SxCR_EN;
    DMA1_Stream1->CR |= DMA_SxCR_EN;

    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;

    TIM1->CNT = 0U;
    TIM1->SR = 0U;
    TIM1->CCER |= TIM_CCER_CC2E;
    TIM1->CR1 |= TIM_CR1_CEN;
    __NOP();
    __NOP();
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
    {
      intan_dma_timcs_recover(old_midi);
      return HAL_TIMEOUT;
    }

    TIM1->CR1 &= ~TIM_CR1_CEN;
    TIM1->CCER &= ~TIM_CCER_CC2E;
    INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

    last_rx = s_dma_rx_words[chunk_slots - 1U];
    Intan_SpiStats_AddXfer32(chunk_slots);
    n -= chunk_slots;
  }

  intan_dma_timcs_recover(old_midi);

  if (last_rx_out != NULL)
  {
    *last_rx_out = last_rx;
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
  uint32_t period_ticks;
#if !INTAN_CS_HW_NSS
  uint32_t low_ticks;
#endif
  uint32_t old_midi;
  uint32_t chunk_slots;
  uint32_t rx_offset;
  uint32_t cmd = intan_convert_cmd_word(channel, flags);

  if (!g_intan_spi_ready || samples == NULL)
  {
    return HAL_ERROR;
  }

  intan_pipeline_layout_remember_poll(n, &chunk_slots, &rx_offset);
  if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
  {
    return HAL_ERROR;
  }

  Intan_SpiDiag_Init();
#if INTAN_CS_HW_NSS
  if (intan_dma_timcs_ensure_armed(0U, 0U, &old_midi) != HAL_OK)
  {
    intan_dma_timcs_recover(old_midi);
    return HAL_ERROR;
  }
  period_ticks = INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES;
#else
  if (intan_dma_compute_tim_ticks(INTAN_DMA_TIMCS_PERIOD_SCK_CYCLES, INTAN_DMA_TIMCS_HIGH_NS,
                                  &period_ticks, &low_ticks) != HAL_OK)
  {
    intan_cs_gpio_mode();
    return HAL_ERROR;
  }
  if (intan_dma_timcs_ensure_armed(period_ticks, low_ticks, &old_midi) != HAL_OK)
  {
    intan_dma_timcs_recover(old_midi);
    return HAL_ERROR;
  }
#endif

  if (intan_dma_prepare_streams(cmd, chunk_slots) != HAL_OK)
  {
    intan_dma_finish_read(old_midi, s_dma_timcs_armed, 1U);
    return HAL_TIMEOUT;
  }

  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = chunk_slots;
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;

  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
#if !INTAN_CS_HW_NSS
  TIM1->CNT = 0U;
  TIM1->SR = 0U;
  TIM1->CCER |= TIM_CCER_CC2E;
#endif
  {
    uint32_t cyc_start = DWT->CYCCNT;
#if !INTAN_CS_HW_NSS
    TIM1->CR1 |= TIM_CR1_CEN;
    __NOP();
    __NOP();
#endif
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
    {
      intan_dma_timcs_recover(old_midi);
      s_dma_timcs_armed = 0U;
      s_convert_pipeline_primed = 0U;
      return HAL_TIMEOUT;
    }

    Intan_SpiDiag_RecordBlock(cyc_start, DWT->CYCCNT, n, chunk_slots, period_ticks);
  }

  intan_unpack_convert_block(samples, n, rx_offset, 0x8000U);

  Intan_SpiStats_AddXfer32(chunk_slots);
  intan_dma_finish_read(old_midi, s_dma_timcs_armed, 1U);
  return HAL_OK;
}

static HAL_StatusTypeDef INTAN_LEGACY_UNUSED intan_timslot_dma_subblock(
    uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples, uint32_t *period_ticks,
    uint32_t *low_ticks, uint32_t *old_midi, uint8_t *tim_ready, uint8_t continuing_sub)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
  uint32_t chunk_slots;
  uint32_t rx_offset;
  uint8_t hot_chain;

  if (samples == NULL || period_ticks == NULL || low_ticks == NULL || old_midi == NULL ||
      tim_ready == NULL)
  {
    return HAL_ERROR;
  }

  hot_chain = (continuing_sub != 0U && intan_timslot_gap_is_cold() == 0U) ? 1U : 0U;
  intan_pipeline_layout_remember_single(n, &chunk_slots, &rx_offset, hot_chain);
  if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
  {
    return HAL_ERROR;
  }

  intan_fill_single_tx_words(n, channel, flags);

  if (*tim_ready == 0U)
  {
#if INTAN_CS_HW_NSS
    if (intan_dma_timslot_ensure_armed(0U, 0U, old_midi) != HAL_OK)
    {
      intan_dma_timcs_recover(*old_midi);
      return HAL_ERROR;
    }
    *period_ticks = INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES;
    *low_ticks = 0U;
#else
    if (intan_dma_compute_tim_ticks(INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES, INTAN_DMA_TIMSLOT_HIGH_NS,
                                    period_ticks, low_ticks) != HAL_OK)
    {
      intan_cs_gpio_mode();
      return HAL_ERROR;
    }
    if (intan_dma_timslot_ensure_armed(*period_ticks, *low_ticks, old_midi) != HAL_OK)
    {
      intan_dma_timcs_recover(*old_midi);
      return HAL_ERROR;
    }
#endif
    *tim_ready = 1U;
  }

  if (intan_dma_prepare_streams_ex(s_dma_tx_words, chunk_slots, 1U, INTAN_DMA_STREAM_TX_REQUEST) !=
      HAL_OK)
  {
    return HAL_TIMEOUT;
  }

  intan_timslot_cs_prepare_burst(hot_chain);

  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = chunk_slots;
#if INTAN_CS_HW_NSS
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;
#else
  INTAN_SPI_INSTANCE->CFG1 &= ~SPI_CFG1_TXDMAEN;
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN;
#endif

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;

  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;

  {
    uint32_t cyc_start = DWT->CYCCNT;
    intan_timslot_burst_enter();
#if !INTAN_CS_HW_NSS
    intan_timslot_tim_kick(1U);
#endif
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
    {
      intan_timslot_burst_exit();
      intan_dma_timcs_recover(*old_midi);
      s_dma_timslot_armed = 0U;
      s_convert_pipeline_primed = 0U;
      return HAL_TIMEOUT;
    }
    intan_timslot_burst_exit();

    Intan_SpiDiag_RecordBlock(cyc_start, DWT->CYCCNT, n, chunk_slots, *period_ticks);
  }

  intan_unpack_convert_block(samples, n, rx_offset, s_stream_tail_adc);

  if (n > 0U)
  {
    s_stream_tail_adc = samples[n - 1U];
  }

  Intan_SpiStats_AddXfer32(chunk_slots);
  intan_timslot_mark_burst_end();
  return HAL_OK;
}

static HAL_StatusTypeDef INTAN_LEGACY_UNUSED intan_timslot_dma_subblock_range(
    uint32_t n, uint8_t first_ch, uint8_t n_ch, uint8_t flags, uint16_t *samples,
    uint8_t *phase_io, uint32_t *period_ticks, uint32_t *low_ticks, uint32_t *old_midi,
    uint8_t *tim_ready, uint8_t continuing_sub)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
  uint32_t chunk_slots;
  uint32_t rx_offset;
  uint8_t phase = 0U;
  uint8_t hot_chain;

  if (samples == NULL || period_ticks == NULL || low_ticks == NULL || old_midi == NULL ||
      tim_ready == NULL || n_ch == 0U || first_ch >= INTAN_STREAM_RR16_CHANNELS ||
      (first_ch + n_ch) > INTAN_STREAM_RR16_CHANNELS)
  {
    return HAL_ERROR;
  }

  if (phase_io != NULL)
  {
    phase = *phase_io;
  }

  hot_chain = (continuing_sub != 0U && intan_timslot_gap_is_cold() == 0U) ? 1U : 0U;
  intan_pipeline_layout_remember_timslot(n, &chunk_slots, &rx_offset, continuing_sub);
  if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
  {
    return HAL_ERROR;
  }

  intan_fill_rr_range_tx_words(n, first_ch, n_ch, flags, phase, (hot_chain == 0U) ? 1U : 0U);

  if (*tim_ready == 0U)
  {
#if INTAN_CS_HW_NSS
    if (intan_dma_timslot_ensure_armed(0U, 0U, old_midi) != HAL_OK)
    {
      intan_dma_timcs_recover(*old_midi);
      return HAL_ERROR;
    }
    *period_ticks = INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES;
    *low_ticks = 0U;
#else
    if (intan_dma_compute_tim_ticks(INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES, INTAN_DMA_TIMSLOT_HIGH_NS,
                                    period_ticks, low_ticks) != HAL_OK)
    {
      intan_cs_gpio_mode();
      return HAL_ERROR;
    }
    if (intan_dma_timslot_ensure_armed(*period_ticks, *low_ticks, old_midi) != HAL_OK)
    {
      intan_dma_timcs_recover(*old_midi);
      return HAL_ERROR;
    }
#endif
    *tim_ready = 1U;
  }

  if (intan_dma_prepare_streams_ex(s_dma_tx_words, chunk_slots, 1U, INTAN_DMA_STREAM_TX_REQUEST) !=
      HAL_OK)
  {
    return HAL_TIMEOUT;
  }

  intan_timslot_cs_prepare_burst(hot_chain);

  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = chunk_slots;
#if INTAN_CS_HW_NSS
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;
#else
  INTAN_SPI_INSTANCE->CFG1 &= ~SPI_CFG1_TXDMAEN;
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN;
#endif

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;

  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;

  {
    uint32_t cyc_start = DWT->CYCCNT;
    intan_timslot_burst_enter();
#if !INTAN_CS_HW_NSS
    intan_timslot_tim_kick(1U);
#endif
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
    {
      intan_timslot_burst_exit();
      intan_dma_timcs_recover(*old_midi);
      s_dma_timslot_armed = 0U;
      s_convert_pipeline_primed = 0U;
      return HAL_TIMEOUT;
    }
    intan_timslot_burst_exit();

    Intan_SpiDiag_RecordBlock(cyc_start, DWT->CYCCNT, n, chunk_slots, *period_ticks);
  }

  intan_unpack_set_rr_context(first_ch, n_ch, phase);
  intan_unpack_rr_sanitize_block(samples, n, rx_offset);

  if (n > 0U && s_dma_stream_channel_count <= 1U)
  {
    s_stream_tail_adc = samples[n - 1U];
  }

  if (phase_io != NULL)
  {
    *phase_io = (uint8_t)((phase + n) % (uint32_t)n_ch);
  }

  Intan_SpiStats_AddXfer32(chunk_slots);
  intan_timslot_mark_burst_end();
  return HAL_OK;
}

static uint8_t intan_timslot_stream_continuing(void)
{
  if (s_dma_stream_continuous == 0U || s_convert_pipeline_primed == 0U)
  {
    return 0U;
  }
  return (intan_timslot_gap_is_cold() == 0U) ? 1U : 0U;
}

static HAL_StatusTypeDef intan_timslot_read_sync(uint8_t is_range, uint32_t n, uint8_t channel,
                                                 uint8_t first_ch, uint8_t n_ch, uint8_t flags,
                                                 uint16_t *samples, uint8_t *phase_io)
{
  HAL_StatusTypeDef st = HAL_OK;
  uint8_t halt_after;
  uint32_t off = 0U;

  halt_after = (s_dma_stream_continuous != 0U) ? 1U : 0U;
  Intan_SpiDiag_Init();

  while ((off < n) && (st == HAL_OK))
  {
    uint32_t sub = n - off;
    uint8_t cont;

    if (sub > INTAN_TIMSLOT_SUBBLOCK_MAX)
    {
      sub = INTAN_TIMSLOT_SUBBLOCK_MAX;
    }

    cont = intan_timslot_stream_continuing();

    if (is_range != 0U)
    {
      st = Intan_StreamDmaStartRange(sub, first_ch, n_ch, flags, &samples[off], phase_io, cont);
    }
    else
    {
      st = Intan_StreamDmaStartSingle(sub, channel, flags, &samples[off], cont);
    }
    if (st != HAL_OK)
    {
      break;
    }

    while (Intan_StreamDmaPoll() == INTAN_STREAM_DMA_RUNNING)
    {
    }

    if (Intan_StreamDmaPoll() != INTAN_STREAM_DMA_DONE)
    {
      Intan_StreamDmaReset();
      st = HAL_TIMEOUT;
      break;
    }

    st = Intan_StreamDmaComplete((off + sub >= n) ? halt_after : 0U);
    off += sub;
  }

  return st;
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotRead(uint32_t n, uint8_t channel, uint8_t flags,
                                                      uint16_t *samples)
{
  if (!g_intan_spi_ready || samples == NULL || n == 0U)
  {
    return HAL_ERROR;
  }

  return intan_timslot_read_sync(0U, n, channel, 0U, 0U, flags, samples, NULL);
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCsReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                      uint16_t *samples, uint8_t *phase_io)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
#if !INTAN_CS_HW_NSS
  uint32_t period_ticks;
  uint32_t low_ticks;
#endif
  uint32_t old_midi;
  uint32_t chunk_slots;
  uint32_t rx_offset;
  uint8_t phase = 0U;
  uint8_t pipeline_cold;

  if (!g_intan_spi_ready || samples == NULL || n_ch == 0U || n_ch > 16U)
  {
    return HAL_ERROR;
  }

  if (phase_io != NULL)
  {
    phase = *phase_io;
  }

  intan_pipeline_layout_remember_poll(n, &chunk_slots, &rx_offset);
  if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
  {
    return HAL_ERROR;
  }

  pipeline_cold = (intan_pipeline_is_hot() != 0U) ? 0U : 1U;
  intan_fill_rr_tx_words(n, n_ch, flags, phase, pipeline_cold);

  Intan_SpiDiag_Init();
#if INTAN_CS_HW_NSS
  if (intan_dma_timcs_ensure_armed(0U, 0U, &old_midi) != HAL_OK)
  {
    intan_dma_timcs_recover(old_midi);
    return HAL_ERROR;
  }
#else
  if (intan_dma_compute_tim_ticks(INTAN_DMA_TIMCS_PERIOD_SCK_CYCLES, INTAN_DMA_TIMCS_HIGH_NS,
                                  &period_ticks, &low_ticks) != HAL_OK)
  {
    intan_cs_gpio_mode();
    return HAL_ERROR;
  }
  if (intan_dma_timcs_ensure_armed(period_ticks, low_ticks, &old_midi) != HAL_OK)
  {
    intan_dma_timcs_recover(old_midi);
    return HAL_ERROR;
  }
#endif

  if (intan_dma_prepare_streams_ex(s_dma_tx_words, chunk_slots, 1U, INTAN_DMA_STREAM_TX_REQUEST) !=
      HAL_OK)
  {
    intan_dma_finish_read(old_midi, s_dma_timcs_armed, 1U);
    return HAL_TIMEOUT;
  }

  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = chunk_slots;
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;

  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
#if !INTAN_CS_HW_NSS
  TIM1->CNT = 0U;
  TIM1->SR = 0U;
  TIM1->CCER |= TIM_CCER_CC2E;
  TIM1->CR1 |= TIM_CR1_CEN;
  __NOP();
  __NOP();
#endif
  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

  if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
      intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
      intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
  {
    intan_dma_timcs_recover(old_midi);
    s_dma_timcs_armed = 0U;
    s_convert_pipeline_primed = 0U;
    return HAL_TIMEOUT;
  }

  intan_unpack_set_rr_context(0U, n_ch, phase);
  intan_unpack_rr_sanitize_block(samples, n, rx_offset);

  if (phase_io != NULL)
  {
    *phase_io = (uint8_t)((phase + n) % (uint32_t)n_ch);
  }

  Intan_SpiStats_AddXfer32(chunk_slots);
  intan_dma_finish_read(old_midi, s_dma_timcs_armed, 1U);
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                        uint16_t *samples, uint8_t *phase_io)
{
  return Intan_ConvertPipelineDmaTimSlotReadRange(n, 0U, n_ch, flags, samples, phase_io);
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimSlotReadRange(uint32_t n, uint8_t first_ch, uint8_t n_ch,
                                                           uint8_t flags, uint16_t *samples,
                                                           uint8_t *phase_io)
{
  if (!g_intan_spi_ready || samples == NULL || n == 0U || n_ch == 0U ||
      first_ch >= INTAN_STREAM_RR16_CHANNELS || (first_ch + n_ch) > INTAN_STREAM_RR16_CHANNELS)
  {
    return HAL_ERROR;
  }

  Intan_SpiDiag_Init();
  return intan_timslot_read_sync(1U, n, 0U, first_ch, n_ch, flags, samples, phase_io);
}

typedef struct {
  uint8_t active;
  uint8_t is_range;
  uint8_t continuing_sub;
  uint8_t channel;
  uint8_t first_ch;
  uint8_t n_ch;
  uint8_t flags;
  uint8_t apply_recover;
  uint8_t rr_phase;
  uint32_t n;
  uint32_t rx_offset;
  uint32_t chunk_slots;
  uint32_t cyc_start;
  uint16_t *samples;
  uint8_t *phase_io;
} IntanStreamDmaJob;

static IntanStreamDmaJob s_stream_job;
static uint8_t s_stream_tim_ready;
static uint32_t s_stream_period_ticks;
static uint32_t s_stream_low_ticks;
static uint32_t s_stream_old_midi;

HAL_StatusTypeDef Intan_SetSpiPrescalerDiv(uint32_t div)
{
  uint32_t br;

  if (s_stream_job.active != 0U)
  {
    return HAL_BUSY;
  }

  switch (div)
  {
    case 2U:
      br = SPI_BAUDRATEPRESCALER_2;
      break;
    case 4U:
      br = SPI_BAUDRATEPRESCALER_4;
      break;
    case 8U:
      br = SPI_BAUDRATEPRESCALER_8;
      break;
    case 16U:
      br = SPI_BAUDRATEPRESCALER_16;
      break;
    case 32U:
      br = SPI_BAUDRATEPRESCALER_32;
      break;
    case 64U:
      br = SPI_BAUDRATEPRESCALER_64;
      break;
    case 128U:
      br = SPI_BAUDRATEPRESCALER_128;
      break;
    case 256U:
      br = SPI_BAUDRATEPRESCALER_256;
      break;
    default:
      return HAL_ERROR;
  }

  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG1, SPI_CFG1_MBR, br);
  s_spi_prescaler_div = div;
  return HAL_OK;
}

static uint8_t intan_stream_dma_hw_done(void)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;

  if ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U)
  {
    return 0U;
  }
  if ((DMA1->LISR & dma_stream0_done) == 0U)
  {
    return 0U;
  }
  if ((DMA1->LISR & dma_stream1_done) == 0U)
  {
    return 0U;
  }
  return 1U;
}

static HAL_StatusTypeDef intan_stream_dma_hw_start(uint32_t chunk_slots, uint8_t hot_chain)
{
  intan_timslot_cs_prepare_burst(hot_chain);

  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = chunk_slots;
#if INTAN_CS_HW_NSS
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;
#else
  INTAN_SPI_INSTANCE->CFG1 &= ~SPI_CFG1_TXDMAEN;
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN;
#endif

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;

  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
  intan_timslot_burst_enter();
#if !INTAN_CS_HW_NSS
  intan_timslot_tim_kick(1U);
#endif
  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;
  s_stream_job.cyc_start = DWT->CYCCNT;
  return HAL_OK;
}

static void intan_stream_dma_unpack(void)
{
  uint32_t i;
  uint16_t *samples = s_stream_job.samples;
  uint16_t prev = s_stream_tail_adc;

  for (i = 0U; i < s_stream_job.n; i++)
  {
    samples[i] = intan_adc_from_convert_rx(s_dma_rx_words[i + s_stream_job.rx_offset], prev);
    prev = samples[i];
  }

  if (s_stream_job.n > 0U)
  {
    s_stream_tail_adc = samples[s_stream_job.n - 1U];
  }

  if (s_stream_job.is_range != 0U && s_stream_job.phase_io != NULL)
  {
    *s_stream_job.phase_io =
        (uint8_t)((s_stream_job.rr_phase + s_stream_job.n) % (uint32_t)s_stream_job.n_ch);
  }
}

static HAL_StatusTypeDef intan_stream_dma_start_job(uint8_t is_range, uint32_t n, uint8_t channel,
                                                    uint8_t first_ch, uint8_t n_ch, uint8_t flags,
                                                    uint16_t *samples, uint8_t *phase_io,
                                                    uint8_t continuing_sub)
{
  uint8_t hot_chain;

  if (!g_intan_spi_ready || samples == NULL || n == 0U)
  {
    return HAL_ERROR;
  }
  if (s_stream_job.active != 0U)
  {
    intan_sample_clip_bump();
    return HAL_BUSY;
  }
  if (intan_stream_dma_hw_done() == 0U && s_timslot_burst_active != 0U)
  {
    intan_sample_clip_bump();
    return HAL_BUSY;
  }

  hot_chain = (continuing_sub != 0U && intan_timslot_gap_is_cold() == 0U) ? 1U : 0U;

  s_stream_job.active = 1U;
  s_stream_job.is_range = is_range;
  s_stream_job.continuing_sub = continuing_sub;
  s_stream_job.channel = channel;
  s_stream_job.first_ch = first_ch;
  s_stream_job.n_ch = n_ch;
  s_stream_job.flags = flags;
  s_stream_job.n = n;
  s_stream_job.samples = samples;
  s_stream_job.phase_io = phase_io;

  if (is_range != 0U)
  {
    intan_pipeline_layout_remember_timslot(n, &s_stream_job.chunk_slots, &s_stream_job.rx_offset,
                                           continuing_sub);
  }
  else
  {
    intan_pipeline_layout_remember_single(n, &s_stream_job.chunk_slots, &s_stream_job.rx_offset,
                                          hot_chain);
  }
  if (intan_pipeline_validate_n(n, s_stream_job.chunk_slots) != HAL_OK)
  {
    s_stream_job.active = 0U;
    return HAL_ERROR;
  }

  if (is_range != 0U)
  {
    if (n_ch == 0U || first_ch >= INTAN_STREAM_RR16_CHANNELS || (first_ch + n_ch) > INTAN_STREAM_RR16_CHANNELS)
    {
      s_stream_job.active = 0U;
      return HAL_ERROR;
    }
    s_stream_job.rr_phase = (phase_io != NULL) ? *phase_io : 0U;
    intan_fill_rr_range_tx_words(n, first_ch, n_ch, flags, s_stream_job.rr_phase,
                                 (hot_chain == 0U) ? 1U : 0U);
  }
  else
  {
    intan_fill_single_tx_words(n, channel, flags);
  }

  if (s_stream_tim_ready == 0U)
  {
    if (intan_dma_compute_tim_ticks(INTAN_DMA_TIMSLOT_PERIOD_SCK_CYCLES, INTAN_DMA_TIMSLOT_HIGH_NS,
                                    &s_stream_period_ticks, &s_stream_low_ticks) != HAL_OK)
    {
      intan_cs_gpio_mode();
      s_stream_job.active = 0U;
      return HAL_ERROR;
    }
    if (intan_dma_timslot_ensure_armed(s_stream_period_ticks, s_stream_low_ticks, &s_stream_old_midi) !=
        HAL_OK)
    {
      intan_dma_timcs_recover(s_stream_old_midi);
      s_stream_job.active = 0U;
      return HAL_ERROR;
    }
    s_stream_tim_ready = 1U;
  }

  if (intan_dma_prepare_streams_ex(s_dma_tx_words, s_stream_job.chunk_slots, 1U,
                                   INTAN_DMA_STREAM_TX_REQUEST) != HAL_OK)
  {
    s_stream_job.active = 0U;
    return HAL_TIMEOUT;
  }

  return intan_stream_dma_hw_start(s_stream_job.chunk_slots, hot_chain);
}

void Intan_StreamDmaReset(void)
{
  if (s_timslot_burst_active != 0U)
  {
    intan_timslot_burst_exit();
  }
  if (s_stream_job.active != 0U)
  {
    intan_dma_finish_read(s_stream_old_midi, s_dma_timslot_armed, 1U);
  }
  memset(&s_stream_job, 0, sizeof(s_stream_job));
  s_stream_tim_ready = 0U;
}

HAL_StatusTypeDef Intan_StreamDmaStartSingle(uint32_t n, uint8_t channel, uint8_t flags,
                                             uint16_t *samples, uint8_t continuing_sub)
{
  return intan_stream_dma_start_job(0U, n, channel, 0U, 0U, flags, samples, NULL, continuing_sub);
}

HAL_StatusTypeDef Intan_StreamDmaStartRange(uint32_t n, uint8_t first_ch, uint8_t n_ch, uint8_t flags,
                                          uint16_t *samples, uint8_t *phase_io, uint8_t continuing_sub)
{
  return intan_stream_dma_start_job(1U, n, 0U, first_ch, n_ch, flags, samples, phase_io,
                                    continuing_sub);
}

IntanStreamDmaState Intan_StreamDmaPoll(void)
{
  if (s_stream_job.active == 0U)
  {
    return INTAN_STREAM_DMA_IDLE;
  }

  if (intan_stream_dma_hw_done() == 0U)
  {
    if (s_idle_hook != NULL && s_timslot_burst_active == 0U)
    {
      s_idle_hook(s_idle_ctx);
    }
    return INTAN_STREAM_DMA_RUNNING;
  }

  return INTAN_STREAM_DMA_DONE;
}

HAL_StatusTypeDef Intan_StreamDmaComplete(uint8_t halt_after)
{
  if (s_stream_job.active == 0U)
  {
    return HAL_ERROR;
  }

  intan_timslot_burst_exit();
  Intan_SpiDiag_RecordBlock(s_stream_job.cyc_start, DWT->CYCCNT, s_stream_job.n,
                            s_stream_job.chunk_slots, s_stream_period_ticks);
  intan_stream_dma_unpack();
  Intan_SpiStats_AddXfer32(s_stream_job.chunk_slots);
  intan_timslot_mark_burst_end();
  intan_dma_finish_read(s_stream_old_midi, s_dma_timslot_armed, halt_after);
  s_stream_job.active = 0U;
  return HAL_OK;
}

static HAL_StatusTypeDef intan_xfer32_repeat_timcs(uint32_t tx_word, uint32_t n, uint32_t target_ksps,
                                                   uint32_t *last_rx_out)
{
  const uint32_t max_slots_per_chunk = 30000U;
  uint32_t tim_clk;
  uint32_t period_ticks;
  uint32_t high_ticks;
  uint32_t low_ticks;
  uint32_t last_rx = 0U;
  __IO uint32_t *txdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->RXDR;

  if (!g_intan_spi_ready || n == 0U)
  {
    return HAL_ERROR;
  }
  if (target_ksps == 0U)
  {
    target_ksps = 600U;
  }

  tim_clk = intan_tim1_clock_hz();
  period_ticks = (tim_clk + (target_ksps * 1000U / 2U)) / (target_ksps * 1000U);
  high_ticks = (tim_clk + 9999999U) / 10000000U; /* >=100 ns CS high. */
  if (high_ticks < 2U)
  {
    high_ticks = 2U;
  }
  if (period_ticks <= high_ticks + 4U)
  {
    period_ticks = high_ticks + 5U;
  }
  low_ticks = period_ticks - high_ticks;

  intan_cs_tim1_ch2_mode();

  TIM1->CR1 = 0U;
  TIM1->CR2 = 0U;
  TIM1->SMCR = 0U;
  TIM1->DIER = 0U;
  TIM1->PSC = 0U;
  TIM1->ARR = period_ticks - 1U;
  TIM1->CCR2 = low_ticks;
  TIM1->CCMR1 &= ~(TIM_CCMR1_OC2M | TIM_CCMR1_CC2S);
  TIM1->CCMR1 |= (6U << TIM_CCMR1_OC2M_Pos) | TIM_CCMR1_OC2PE; /* PWM1. */
  TIM1->CCER &= ~TIM_CCER_CC2E;
  TIM1->CCER |= TIM_CCER_CC2P; /* Active low while CNT < CCR2. */
  TIM1->BDTR |= TIM_BDTR_MOE;
  TIM1->EGR = TIM_EGR_UG;
  TIM1->SR = 0U;

  while (n != 0U)
  {
    uint32_t chunk_slots = (n > max_slots_per_chunk) ? max_slots_per_chunk : n;
    uint32_t i;

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = chunk_slots;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    TIM1->CNT = 0U;
    TIM1->SR = 0U;
    TIM1->CCER |= TIM_CCER_CC2E;
    TIM1->CR1 |= TIM_CR1_CEN;

    for (i = 0U; i < chunk_slots; i++)
    {
      if (intan_wait_reg_flag_guard(&TIM1->SR, TIM_SR_UIF) != HAL_OK)
      {
        intan_timcs_recover();
        return HAL_TIMEOUT;
      }
      TIM1->SR = (uint16_t)~TIM_SR_UIF;

      if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_TXP) != HAL_OK)
      {
        intan_timcs_recover();
        return HAL_TIMEOUT;
      }
      *txdr32 = tx_word;

      if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_RXP) != HAL_OK)
      {
        intan_timcs_recover();
        return HAL_TIMEOUT;
      }
      last_rx = *rxdr32;
    }

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK)
    {
      intan_timcs_recover();
      return HAL_TIMEOUT;
    }
    TIM1->CR1 &= ~TIM_CR1_CEN;
    TIM1->CCER &= ~TIM_CCER_CC2E;
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    n -= chunk_slots;
  }

  intan_timcs_recover();

  if (last_rx_out != NULL)
  {
    *last_rx_out = last_rx;
  }
  return HAL_OK;
}

void Intan_SPI_Init(SPI_HandleTypeDef *hspi)
{
  if (hspi == NULL || hspi->Instance != INTAN_SPI_INSTANCE)
  {
    g_intan_spi_ready = 0U;
    return;
  }
  g_intan_spi_ready = 1U;

#if !INTAN_CS_HW_NSS
  __HAL_RCC_GPIOE_CLK_ENABLE();
  intan_cs_gpio_mode();
#endif
}

uint8_t Intan_SPI_IsReady(void)
{
  return g_intan_spi_ready;
}

/*
 * Даташит RHS2116: SPI с конвейером — 32-бит ответ на команду по MISO приходит через
 * два полных 32-бит цикла позже (слот 1 → результат в слоте 3). Поэтому READ(R) — это
 * три отдельных CS-транзакции: (1) слово READ, (2) нули, (3) нули и приём D[15:0] в младших
 * 16 битах слова. CS между слотами high; при CS high MISO у чипа Hi-Z (см. диаграмму).
 */
static HAL_StatusTypeDef intan_read_reg_impl(uint8_t reg_addr, uint16_t *value, uint32_t *raw32_out)
{
  HAL_StatusTypeDef st;
  uint32_t rx;

  st = intan_xfer32(intan_pack_be4(0xC0U, reg_addr, 0x00U, 0x00U), NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (raw32_out != NULL)
  {
    *raw32_out = rx;
  }
  if (value != NULL)
  {
    *value = intan_u16_from_read_word(rx);
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ReadReg(uint8_t reg_addr, uint16_t *value)
{
  return intan_read_reg_impl(reg_addr, value, NULL);
}

HAL_StatusTypeDef Intan_ReadReg_WithRaw(uint8_t reg_addr, uint16_t *value, uint32_t *raw32_out)
{
  return intan_read_reg_impl(reg_addr, value, raw32_out);
}

HAL_StatusTypeDef Intan_Xfer32Word(uint32_t tx_word, uint32_t *rx_out)
{
  return intan_xfer32(tx_word, rx_out);
}

HAL_StatusTypeDef Intan_WriteReg(uint8_t reg_addr, uint16_t value, uint8_t u_flag, uint8_t m_flag)
{
  HAL_StatusTypeDef st;
  uint8_t h = (uint8_t)(0x80U | ((u_flag & 1U) << 5) | ((m_flag & 1U) << 4));
  uint8_t vhi = (uint8_t)((value >> 8) & 0xFFU);
  uint8_t vlo = (uint8_t)(value & 0xFFU);

  st = intan_xfer32(intan_pack_be4(h, reg_addr, vhi, vlo), NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  return intan_xfer32(0U, NULL);
}

HAL_StatusTypeDef Intan_Convert(uint8_t channel, uint8_t flags, uint16_t *value)
{
  HAL_StatusTypeDef st;
  uint32_t rx;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);

  st = intan_xfer32(intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                                   0x00U, 0x00U),
                    NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (value != NULL)
  {
    *value = intan_u16_from_convert_word(rx);
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipeline(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *last_value)
{
  HAL_StatusTypeDef st;
  uint32_t rx = 0U;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);
  uint32_t cmd = intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                                0x00U, 0x00U);

  if (n == 0U)
  {
    return HAL_ERROR;
  }

  /* Prime and timed acquisition are one tight loop: two pipeline slots + n returned samples. */
  st = intan_xfer32_repeat_fast(cmd, n + 2U, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (last_value != NULL)
  {
    *last_value = intan_u16_from_convert_word(rx);
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples)
{
  uint32_t rx = 0U;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);
  uint32_t cmd = intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                                0x00U, 0x00U);
  __IO uint32_t *txdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->RXDR;
#if !INTAN_CS_HW_NSS
  const uint32_t cs_set = (uint32_t)INTAN_CS_PIN;
  const uint32_t cs_reset = ((uint32_t)INTAN_CS_PIN << 16);
#endif
  uint32_t slots;

  if (!g_intan_spi_ready || samples == NULL)
  {
    return HAL_ERROR;
  }

  {
    uint32_t chunk_slots;
    uint32_t rx_offset;
    uint8_t hot_chain = intan_pipeline_is_hot();
    intan_pipeline_layout_remember_single(n, &chunk_slots, &rx_offset, hot_chain);
    if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
    {
      return HAL_ERROR;
    }
    slots = chunk_slots;

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = slots;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    for (uint32_t i = 0U; i < slots; i++)
    {
#if !INTAN_CS_HW_NSS
      INTAN_CS_GPIO_PORT->BSRR = cs_reset;
      intan_poll_pipe_delay();
#endif

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_TXP) == 0U) {}
      *txdr32 = cmd;

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_RXP) == 0U) {}
      rx = *rxdr32;
#if !INTAN_CS_HW_NSS
      intan_poll_pipe_delay();
      INTAN_CS_GPIO_PORT->BSRR = cs_set;
      intan_poll_pipe_delay();
#endif

      if (i >= rx_offset)
      {
        uint32_t out_i = i - rx_offset;
        uint16_t prev = (out_i > 0U) ? samples[out_i - 1U] : 0x8000U;

        samples[out_i] = intan_adc_from_convert_rx(rx, prev);
      }
    }

    while ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U) {}
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

    Intan_SpiStats_AddXfer32(slots);
    intan_pipeline_mark_done();
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                              uint16_t *samples, uint8_t *phase_io)
{
  uint32_t rx = 0U;
  __IO uint32_t *txdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->RXDR;
#if !INTAN_CS_HW_NSS
  const uint32_t cs_set = (uint32_t)INTAN_CS_PIN;
  const uint32_t cs_reset = ((uint32_t)INTAN_CS_PIN << 16);
#endif
  uint32_t slots;
  uint8_t phase = 0U;

  if (!g_intan_spi_ready || samples == NULL || n_ch == 0U || n_ch > 16U)
  {
    return HAL_ERROR;
  }

  if (phase_io != NULL)
  {
    phase = *phase_io;
  }

  {
    uint32_t chunk_slots;
    uint32_t rx_offset;
    intan_pipeline_layout_remember_poll(n, &chunk_slots, &rx_offset);
    if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
    {
      return HAL_ERROR;
    }
    slots = chunk_slots;

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = slots;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    for (uint32_t i = 0U; i < slots; i++)
    {
      uint32_t cmd = intan_convert_cmd_word((uint8_t)((phase + i) % n_ch), flags);

#if !INTAN_CS_HW_NSS
      INTAN_CS_GPIO_PORT->BSRR = cs_reset;
      intan_poll_pipe_delay();
#endif

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_TXP) == 0U) {}
      *txdr32 = cmd;

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_RXP) == 0U) {}
      rx = *rxdr32;
#if !INTAN_CS_HW_NSS
      intan_poll_pipe_delay();
      INTAN_CS_GPIO_PORT->BSRR = cs_set;
      intan_poll_pipe_delay();
#endif

      if (i >= rx_offset)
      {
        uint32_t out_i = i - rx_offset;
        uint8_t ch = (uint8_t)((phase + out_i) % n_ch);
        uint16_t prev = s_stream_tail_adc_ch[ch];
        uint16_t adc = intan_adc_from_convert_rx(rx, prev);

        samples[out_i] = adc;
        s_stream_tail_adc_ch[ch] = adc;
      }

      if ((s_idle_hook != NULL) && ((i & 0x0FU) == 0U))
      {
        s_idle_hook(s_idle_ctx);
      }
    }

    while ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U) {}
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

    Intan_SpiStats_AddXfer32(slots);
    intan_pipeline_mark_done();
  }

  if (phase_io != NULL)
  {
    *phase_io = (uint8_t)((phase + n) % (uint32_t)n_ch);
  }

  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineReadRange(uint32_t n, uint8_t first_ch, uint8_t n_ch,
                                                 uint8_t flags, uint16_t *samples, uint8_t *phase_io)
{
  uint32_t rx = 0U;
  __IO uint32_t *txdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->TXDR;
  __IO uint32_t *rxdr32 = (__IO uint32_t *)&INTAN_SPI_INSTANCE->RXDR;
#if !INTAN_CS_HW_NSS
  const uint32_t cs_set = (uint32_t)INTAN_CS_PIN;
  const uint32_t cs_reset = ((uint32_t)INTAN_CS_PIN << 16);
#endif
  uint32_t slots;
  uint8_t phase = 0U;

  if (!g_intan_spi_ready || samples == NULL || n_ch == 0U || n_ch > 16U ||
      first_ch >= INTAN_STREAM_RR16_CHANNELS || (first_ch + n_ch) > INTAN_STREAM_RR16_CHANNELS)
  {
    return HAL_ERROR;
  }

  if (phase_io != NULL)
  {
    phase = *phase_io;
  }

  {
    uint32_t chunk_slots;
    uint32_t rx_offset;
    intan_pipeline_layout_remember_poll(n, &chunk_slots, &rx_offset);
    if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
    {
      return HAL_ERROR;
    }
    slots = chunk_slots;

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = slots;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

    for (uint32_t i = 0U; i < slots; i++)
    {
      uint8_t ch = (uint8_t)(first_ch + ((phase + i) % (uint32_t)n_ch));
      uint32_t cmd = intan_convert_cmd_word(ch, flags);

#if !INTAN_CS_HW_NSS
      INTAN_CS_GPIO_PORT->BSRR = cs_reset;
      intan_poll_pipe_delay();
#endif

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_TXP) == 0U) {}
      *txdr32 = cmd;

      while ((INTAN_SPI_INSTANCE->SR & SPI_SR_RXP) == 0U) {}
      rx = *rxdr32;
#if !INTAN_CS_HW_NSS
      intan_poll_pipe_delay();
      INTAN_CS_GPIO_PORT->BSRR = cs_set;
      intan_poll_pipe_delay();
#endif

      if (i >= rx_offset)
      {
        uint32_t out_i = i - rx_offset;
        uint8_t ch = (uint8_t)(first_ch + ((phase + out_i) % (uint32_t)n_ch));
        uint16_t prev = s_stream_tail_adc_ch[ch];
        uint16_t adc = intan_adc_from_convert_rx(rx, prev);

        samples[out_i] = adc;
        s_stream_tail_adc_ch[ch] = adc;
      }

      if ((s_idle_hook != NULL) && ((i & 0x0FU) == 0U))
      {
        s_idle_hook(s_idle_ctx);
      }
    }

    while ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U) {}
    INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;

    Intan_SpiStats_AddXfer32(slots);
    intan_pipeline_mark_done();
  }

  if (phase_io != NULL)
  {
    *phase_io = (uint8_t)((phase + n) % (uint32_t)n_ch);
  }

  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineSafeRead(uint32_t n, uint8_t channel, uint8_t flags, uint16_t *samples)
{
  HAL_StatusTypeDef st;
  uint32_t cmd = intan_convert_cmd_word(channel, flags);
  uint32_t rx = 0U;
  uint32_t slots;

  if (!g_intan_spi_ready || samples == NULL)
  {
    return HAL_ERROR;
  }

  {
    uint32_t chunk_slots;
    uint32_t rx_offset;
    uint8_t hot_chain = intan_pipeline_is_hot();
    intan_pipeline_layout_remember_single(n, &chunk_slots, &rx_offset, hot_chain);
    if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
    {
      return HAL_ERROR;
    }
    slots = chunk_slots;

    for (uint32_t i = 0U; i < slots; i++)
    {
      st = intan_xfer32(cmd, &rx);
      if (st != HAL_OK)
      {
        return st;
      }

      if (i >= rx_offset)
      {
        uint32_t out_i = i - rx_offset;
        uint16_t prev = (out_i > 0U) ? samples[out_i - 1U] : 0x8000U;

        samples[out_i] = intan_adc_from_convert_rx(rx, prev);
      }

      if ((s_idle_hook != NULL) && ((i & 0x0FU) == 0U))
      {
        s_idle_hook(s_idle_ctx);
      }
    }

    intan_pipeline_mark_done();
  }

  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineSafeReadRR(uint32_t n, uint8_t n_ch, uint8_t flags,
                                                  uint16_t *samples, uint8_t *phase_io)
{
  HAL_StatusTypeDef st;
  uint8_t phase = 0U;
  uint32_t rx = 0U;
  uint32_t slots;

  if (!g_intan_spi_ready || samples == NULL || n_ch == 0U || n_ch > 16U)
  {
    return HAL_ERROR;
  }

  if (phase_io != NULL)
  {
    phase = *phase_io;
  }

  {
    uint32_t chunk_slots;
    uint32_t rx_offset;
    intan_pipeline_layout_remember_poll(n, &chunk_slots, &rx_offset);
    if (intan_pipeline_validate_n(n, chunk_slots) != HAL_OK)
    {
      return HAL_ERROR;
    }
    slots = chunk_slots;

    for (uint32_t i = 0U; i < slots; i++)
    {
      uint8_t ch = (uint8_t)((phase + i) % n_ch);

      st = intan_xfer32(intan_convert_cmd_word(ch, flags), &rx);
      if (st != HAL_OK)
      {
        return st;
      }

      if (i >= rx_offset)
      {
        uint32_t out_i = i - rx_offset;
        uint16_t prev = s_stream_tail_adc_ch[ch];
        uint16_t adc = intan_adc_from_convert_rx(rx, prev);

        samples[out_i] = adc;
        s_stream_tail_adc_ch[ch] = adc;
      }

      if ((s_idle_hook != NULL) && ((i & 0x0FU) == 0U))
      {
        s_idle_hook(s_idle_ctx);
      }
    }

    intan_pipeline_mark_done();
  }

  if (phase_io != NULL)
  {
    *phase_io = (uint8_t)((phase + n) % (uint32_t)n_ch);
  }

  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                             uint32_t target_ksps, uint16_t *last_value)
{
  HAL_StatusTypeDef st;
  uint32_t rx = 0U;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);
  uint32_t cmd = intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                                0x00U, 0x00U);

  if (n == 0U)
  {
    return HAL_ERROR;
  }

  st = intan_xfer32_repeat_timcs(cmd, n + 2U, target_ksps, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (last_value != NULL)
  {
    *last_value = intan_u16_from_convert_word(rx);
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_ConvertPipelineDmaTimCs(uint32_t n, uint8_t channel, uint8_t flags,
                                                uint16_t *last_value)
{
  HAL_StatusTypeDef st;
  uint32_t rx = 0U;
  uint8_t d_flag = (uint8_t)((flags >> 1) & 1U);
  uint8_t h_flag = (uint8_t)(flags & 1U);
  uint32_t cmd = intan_pack_be4((uint8_t)((d_flag << 3) | (h_flag << 2)), (uint8_t)(channel & 0x3FU),
                                0x00U, 0x00U);

  if (n == 0U)
  {
    return HAL_ERROR;
  }

  st = intan_xfer32_repeat_dma_timcs(cmd, n + 2U, &rx);
  if (st != HAL_OK)
  {
    return st;
  }

  if (last_value != NULL)
  {
    *last_value = intan_u16_from_convert_word(rx);
  }
  return HAL_OK;
}

HAL_StatusTypeDef Intan_RawCmd(const uint8_t cmd4[4])
{
  HAL_StatusTypeDef st;

  if (cmd4 == NULL)
  {
    return HAL_ERROR;
  }

  st = intan_xfer32(intan_pack_be_buf(cmd4), NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  st = intan_xfer32(0U, NULL);
  if (st != HAL_OK)
  {
    return st;
  }
  return intan_xfer32(0U, NULL);
}

HAL_StatusTypeDef Intan_ClearComplianceMonitor(void)
{
  const uint8_t cmd[4] = {0xD0U, 255U, 0x00U, 0x00U};
  return Intan_RawCmd(cmd);
}

/* Снять «слабый» MISO / лишние старшие биты в R1 (см. msu CHANGELOG: 0x951A → 0x051A). */
#define INTAN_R1_STRONG_MISO_CLR_MSK 0x9000U

HAL_StatusTypeDef Intan_ChipBringup(void)
{
  HAL_StatusTypeDef st;
  static const uint8_t clear_adc[4] = {0x6AU, 0x00U, 0x00U, 0x00U};

  st = Intan_RawCmd(clear_adc);
  if (st != HAL_OK)
  {
    return st;
  }
  HAL_Delay(2U);

  st = Intan_WriteReg(38U, 0xFFFFU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }

  /*
   * Do not derive R1 from a read while MISO may still be weak/noisy:
   * write the known-good profile from the datasheet bench sequence directly.
   */
  st = Intan_WriteReg(1U, 0x051AU, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }

  return HAL_OK;
}

static const uint8_t intan_sine64[64] = {
    128, 140, 152, 164, 176, 187, 198, 209, 218, 227, 235, 242, 248, 253, 255, 255,
    255, 255, 253, 248, 242, 235, 227, 218, 209, 198, 187, 176, 164, 152, 140, 128,
    116, 104, 92, 80, 69, 58, 47, 38, 29, 21, 14, 8, 3, 1, 1, 1,
    1, 1, 3, 8, 14, 21, 29, 38, 47, 58, 69, 80, 92, 104, 116, 128,
};

static HAL_StatusTypeDef intan_zcheck_safe_state(void)
{
  HAL_StatusTypeDef st;

  st = Intan_WriteReg(2U, 0x0000U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  return Intan_WriteReg(3U, 0x0080U, 0U, 0U);
}

HAL_StatusTypeDef Intan_MeasureImpedance(IntanImpedanceArg *arg)
{
  uint8_t channel;
  uint8_t scale_bits;
  uint8_t num_samples;
  uint16_t reg2;
  const uint8_t clear_cmd[4] = {0x6AU, 0x00U, 0x00U, 0x00U};
  HAL_StatusTypeDef st;
  unsigned int i;

  if (arg == NULL)
  {
    return HAL_ERROR;
  }

  channel = (uint8_t)(arg->channel & 0x0FU);
  scale_bits = arg->scale_bits;
  num_samples = arg->num_samples;

  if (num_samples == 0U || num_samples > INTAN_IMPEDANCE_MAX_SAMPLES)
  {
    return HAL_ERROR;
  }
  if (scale_bits != 0U && scale_bits != 1U && scale_bits != 3U)
  {
    return HAL_ERROR;
  }

  st = Intan_RawCmd(clear_cmd);
  if (st != HAL_OK)
  {
    return st;
  }

  st = Intan_WriteReg(2U, 0x0040U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }
  st = Intan_WriteReg(3U, 0x0080U, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }

  reg2 = (uint16_t)(((uint16_t)channel << 8) | (1U << 6) | (1U << 0) | ((uint16_t)scale_bits << 3));
  st = Intan_WriteReg(2U, reg2, 0U, 0U);
  if (st != HAL_OK)
  {
    return st;
  }

  for (i = 0U; i < (unsigned int)num_samples; i++)
  {
    uint8_t dac_val;
    uint16_t adc_val;
    unsigned int idx = (i * 64U) / (unsigned int)num_samples;

    if (num_samples == 64U)
    {
      dac_val = intan_sine64[i];
    }
    else
    {
      dac_val = intan_sine64[idx];
    }

    st = Intan_WriteReg(3U, (uint16_t)(dac_val & 0xFFU), 0U, 0U);
    if (st != HAL_OK)
    {
      (void)intan_zcheck_safe_state();
      return st;
    }
    st = Intan_Convert(channel, 0U, &adc_val);
    if (st != HAL_OK)
    {
      (void)intan_zcheck_safe_state();
      return st;
    }
    arg->samples[i] = adc_val;
  }

  return intan_zcheck_safe_state();
}

#define INTAN_IMP_FLAG_PHASE_SAFE    0x01U
#define INTAN_IMP_FLAG_RESTORE_REGS  0x02U

static HAL_StatusTypeDef intan_restore_impedance_regs(const uint16_t regs[9])
{
  HAL_StatusTypeDef st;

  st = Intan_WriteReg(1U, regs[0], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(2U, regs[1], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(3U, regs[2], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(32U, regs[3], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(33U, regs[4], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(42U, regs[5], 1U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(44U, regs[6], 0U, 0U);
  if (st != HAL_OK) { return st; }
  st = Intan_WriteReg(46U, regs[7], 0U, 0U);
  if (st != HAL_OK) { return st; }
  return Intan_WriteReg(48U, regs[8], 0U, 0U);
}

static void intan_impedance_period_sums(uint16_t samples_per_period, int32_t *period_sum_s,
                                        int32_t *period_sum_c)
{
  int32_t sum_s = 0;
  int32_t sum_c = 0;

  for (uint32_t phase_idx = 0U; phase_idx < (uint32_t)samples_per_period; phase_idx++)
  {
    uint32_t basis_idx = (phase_idx * 64U) / (uint32_t)samples_per_period;

    sum_s += (int32_t)intan_sine64[basis_idx] - 128;
    sum_c += (int32_t)intan_sine64[(basis_idx + 16U) & 63U] - 128;
  }

  *period_sum_s = sum_s;
  *period_sum_c = sum_c;
}

static void intan_impedance_accumulate(IntanImpedanceTimedResult *result, uint16_t adc,
                                       uint32_t sample_index, uint16_t samples_per_period,
                                       int32_t period_sum_s, int32_t period_sum_c)
{
  uint32_t basis_idx = ((sample_index % samples_per_period) * 64U) / (uint32_t)samples_per_period;
  int32_t sin_raw = (int32_t)intan_sine64[basis_idx] - 128;
  int32_t cos_raw = (int32_t)intan_sine64[(basis_idx + 16U) & 63U] - 128;
  int32_t sin_basis = (sin_raw * (int32_t)samples_per_period) - period_sum_s;
  int32_t cos_basis = (cos_raw * (int32_t)samples_per_period) - period_sum_c;
  int32_t centered = (int32_t)adc - 32768;

  if (adc < result->adc_min) { result->adc_min = adc; }
  if (adc > result->adc_max) { result->adc_max = adc; }
  if (adc == 0U || adc == 0xFFFFU) { result->clipped++; }
  result->sin_accum += (int64_t)centered * (int64_t)sin_basis;
  result->cos_accum += (int64_t)centered * (int64_t)cos_basis;
  result->adc_sum += adc;
  result->sample_count++;
}

static HAL_StatusTypeDef intan_impedance_prepare_loop(uint8_t channel)
{
  uint16_t dummy = 0U;
  HAL_StatusTypeDef st;
  uint32_t i;

  st = intan_convert_adc_fast(channel, 1U, &dummy);
  if (st != HAL_OK)
  {
    return st;
  }

  for (i = 0U; i < 3U; i++)
  {
    st = intan_write_reg3_fast(128U);
    if (st != HAL_OK)
    {
      return st;
    }
    st = intan_convert_adc_fast(channel, 0U, &dummy);
    if (st != HAL_OK)
    {
      return st;
    }
  }
  return HAL_OK;
}

#define INTAN_IMPEDANCE_SLOTS_PER_SAMPLE  4U

/*
 * Zcheck sample: WRITE Reg3 + CONVERT (3 CS-кадра) = 4 TIM-слота, CS↑ между каждым.
 * ADC — старшие 16 бит ответа на третьем CONVERT-кадре.
 */
static HAL_StatusTypeDef intan_impedance_run_dma_timslot(const IntanImpedanceTimedArg *arg,
                                                         IntanImpedanceTimedResult *result,
                                                         uint32_t sample_count, uint32_t sample_hz,
                                                         int32_t period_sum_s, int32_t period_sum_c)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;
  uint32_t period_ticks;
  uint32_t low_ticks;
  uint32_t old_midi;
  uint32_t chunk_slots;
  uint32_t conv_cmd = intan_convert_cmd_word(arg->channel, 0U);
  uint32_t spp = (uint32_t)arg->samples_per_period;
  uint32_t sample_off = 0U;
  uint32_t total_start;
  HAL_StatusTypeDef st = HAL_OK;

  if (sample_count == 0U || sample_hz == 0U)
  {
    return HAL_ERROR;
  }

  if (intan_dma_compute_tim_ticks_from_slot_hz(sample_hz, &period_ticks, &low_ticks) != HAL_OK)
  {
    return HAL_ERROR;
  }

  total_start = DWT->CYCCNT;

  while (sample_off < sample_count && st == HAL_OK)
  {
    uint32_t chunk_samples = sample_count - sample_off;
    uint32_t max_chunk = INTAN_DMA_CHUNK_SLOTS / INTAN_IMPEDANCE_SLOTS_PER_SAMPLE;

    if (chunk_samples > max_chunk)
    {
      chunk_samples = max_chunk;
    }

    chunk_slots = chunk_samples * INTAN_IMPEDANCE_SLOTS_PER_SAMPLE;
    for (uint32_t j = 0U; j < chunk_samples; j++)
    {
      uint32_t sample_index = sample_off + j;
      uint32_t basis_idx = ((sample_index % spp) * 64U) / spp;
      uint32_t base = j * INTAN_IMPEDANCE_SLOTS_PER_SAMPLE;

      s_dma_tx_words[base + 0U] = intan_write_reg3_word(intan_sine64[basis_idx]);
      s_dma_tx_words[base + 1U] = conv_cmd;
      s_dma_tx_words[base + 2U] = 0U;
      s_dma_tx_words[base + 3U] = 0U;
    }

    if (intan_dma_timslot_ensure_armed(period_ticks, low_ticks, &old_midi) != HAL_OK)
    {
      st = HAL_ERROR;
      break;
    }

    if (intan_dma_prepare_streams_ex(s_dma_tx_words, chunk_slots, 1U, INTAN_DMA_STREAM_TX_REQUEST) !=
        HAL_OK)
    {
      st = HAL_TIMEOUT;
      break;
    }

    INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
    MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
    INTAN_SPI_INSTANCE->IER = 0U;
    INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
    INTAN_SPI_INSTANCE->CR2 = chunk_slots;
#if INTAN_CS_HW_NSS
    INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;
#else
    INTAN_SPI_INSTANCE->CFG1 &= ~SPI_CFG1_TXDMAEN;
    INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN;
#endif

    DMA1_Stream0->CR |= DMA_SxCR_EN;
    DMA1_Stream1->CR |= DMA_SxCR_EN;

    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
    INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;

#if !INTAN_CS_HW_NSS
    TIM1->CNT = 0U;
    TIM1->SR = 0U;
    TIM1->CCER |= TIM_CCER_CC2E;
    TIM1->DIER = TIM_DIER_UDE;
    TIM1->CR1 |= TIM_CR1_CEN;
#endif

    if (intan_wait_reg_flag_guard(&INTAN_SPI_INSTANCE->SR, SPI_SR_EOT) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream0_done) != HAL_OK ||
        intan_wait_reg_flag_guard(&DMA1->LISR, dma_stream1_done) != HAL_OK)
    {
      st = HAL_TIMEOUT;
      break;
    }

    for (uint32_t j = 0U; j < chunk_samples; j++)
    {
      uint16_t adc_val =
          intan_u16_from_convert_word(s_dma_rx_words[(j * INTAN_IMPEDANCE_SLOTS_PER_SAMPLE) + 3U]);

      intan_impedance_accumulate(result, adc_val, sample_off + j, arg->samples_per_period,
                                 period_sum_s, period_sum_c);
    }

    Intan_SpiStats_AddXfer32(chunk_slots);
    intan_dma_timcs_recover(old_midi);
    s_dma_timslot_armed = 0U;
    sample_off += chunk_samples;
  }

  result->elapsed_cycles = DWT->CYCCNT - total_start;
  if (st == HAL_OK && result->sample_count != 0U && spp != 0U)
  {
    result->actual_freq_millihz = (sample_hz * 1000U) / spp;
  }

  return st;
}

static HAL_StatusTypeDef intan_impedance_run_paced_samples_sw(const IntanImpedanceTimedArg *arg,
                                                              IntanImpedanceTimedResult *result,
                                                              uint32_t sample_count,
                                                              uint32_t sample_hz,
                                                              int32_t period_sum_s,
                                                              int32_t period_sum_c)
{
  uint32_t sample_period_cycles;
  uint32_t next_sample;
  uint32_t total_start;
  uint32_t spp = (uint32_t)arg->samples_per_period;

  if (sample_hz == 0U || sample_count == 0U)
  {
    return HAL_ERROR;
  }

  sample_period_cycles = (uint32_t)(((uint64_t)SystemCoreClock + (sample_hz / 2U)) / sample_hz);
  if (sample_period_cycles == 0U)
  {
    return HAL_ERROR;
  }

  total_start = DWT->CYCCNT;
  next_sample = total_start;

  for (uint32_t sample_index = 0U; sample_index < sample_count; sample_index++)
  {
    uint32_t basis_idx = ((sample_index % spp) * 64U) / spp;
    uint8_t dac_val = intan_sine64[basis_idx];
    uint16_t adc_val = 0U;
    uint32_t convert_done;
    HAL_StatusTypeDef st;

    while ((int32_t)(DWT->CYCCNT - next_sample) < 0) {}
    if (sample_index > 0U &&
        (uint32_t)(DWT->CYCCNT - next_sample) > (sample_period_cycles + (sample_period_cycles >> 3)))
    {
      result->overruns++;
    }

    st = intan_write_reg3_fast(dac_val);
    if (st != HAL_OK)
    {
      result->spi_errors++;
      result->elapsed_cycles = DWT->CYCCNT - total_start;
      return HAL_ERROR;
    }

    st = intan_convert_adc_fast(arg->channel, 0U, &adc_val);
    if (st != HAL_OK)
    {
      result->spi_errors++;
      result->elapsed_cycles = DWT->CYCCNT - total_start;
      return HAL_ERROR;
    }

    intan_impedance_accumulate(result, adc_val, sample_index, arg->samples_per_period,
                               period_sum_s, period_sum_c);

    convert_done = DWT->CYCCNT;
    next_sample = convert_done + sample_period_cycles;
  }

  result->elapsed_cycles = DWT->CYCCNT - total_start;
  if (result->elapsed_cycles != 0U && result->sample_count != 0U)
  {
    result->actual_freq_millihz =
        (uint32_t)(((uint64_t)result->sample_count * (uint64_t)SystemCoreClock * 1000ULL) /
                   ((uint64_t)result->elapsed_cycles * (uint64_t)spp));
  }
  return HAL_OK;
}

static HAL_StatusTypeDef intan_impedance_run_paced_samples(const IntanImpedanceTimedArg *arg,
                                                           IntanImpedanceTimedResult *result,
                                                           uint32_t sample_count, uint32_t sample_hz,
                                                           int32_t period_sum_s, int32_t period_sum_c)
{
  uint32_t period_ticks;
  uint32_t low_ticks;

  if (intan_dma_compute_tim_ticks_from_slot_hz(sample_hz, &period_ticks, &low_ticks) == HAL_OK)
  {
    return intan_impedance_run_dma_timslot(arg, result, sample_count, sample_hz, period_sum_s,
                                           period_sum_c);
  }

  return intan_impedance_run_paced_samples_sw(arg, result, sample_count, sample_hz, period_sum_s,
                                              period_sum_c);
}

HAL_StatusTypeDef Intan_MeasureImpedanceTimed(const IntanImpedanceTimedArg *arg,
                                              IntanImpedanceTimedResult *result)
{
  static const uint8_t regs_to_save[9] = {1U, 2U, 3U, 32U, 33U, 42U, 44U, 46U, 48U};
  uint16_t saved[9] = {0};
  HAL_StatusTypeDef st;
  uint32_t sample_count;
  uint32_t slot_hz;
  uint32_t t_elapsed;
  uint16_t reg2;
  uint32_t i;

  if (arg == NULL || result == NULL)
  {
    return HAL_ERROR;
  }
  if (!g_intan_spi_ready || arg->channel >= 16U ||
      (arg->scale_bits != 0U && arg->scale_bits != 1U && arg->scale_bits != 3U) ||
      arg->freq_hz < 10U || arg->freq_hz > 10000U ||
      arg->samples_per_period < 4U || arg->samples_per_period > 128U ||
      arg->periods == 0U || arg->periods > 1000U)
  {
    return HAL_ERROR;
  }

  sample_count = (uint32_t)arg->samples_per_period * (uint32_t)arg->periods;
  slot_hz = (uint32_t)arg->freq_hz * (uint32_t)arg->samples_per_period;
  if (sample_count == 0U || slot_hz == 0U || slot_hz > 200000U)
  {
    return HAL_ERROR;
  }

  memset(result, 0, sizeof(*result));
  result->adc_min = 0xFFFFU;

  for (i = 0U; i < 9U; i++)
  {
    st = Intan_ReadReg(regs_to_save[i], &saved[i]);
    if (st != HAL_OK)
    {
      result->spi_errors++;
      return st;
    }
  }

  st = Intan_WriteReg(2U, 0x0000U, 0U, 0U);
  if (st == HAL_OK) { st = Intan_WriteReg(3U, 0x0080U, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(44U, 0x0000U, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(46U, 0x0000U, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(48U, 0x0000U, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(42U, 0x0000U, 1U, 0U); }
  if (st == HAL_OK) { st = Intan_ClearComplianceMonitor(); }
  if (st != HAL_OK)
  {
    result->spi_errors++;
    (void)intan_zcheck_safe_state();
    if ((arg->flags & INTAN_IMP_FLAG_RESTORE_REGS) != 0U) { (void)intan_restore_impedance_regs(saved); }
    return st;
  }

  if ((arg->flags & INTAN_IMP_FLAG_PHASE_SAFE) != 0U)
  {
    st = Intan_WriteReg(1U, (uint16_t)(saved[0] & ~0x003FU), 0U, 0U);
    if (st != HAL_OK)
    {
      result->spi_errors++;
      (void)intan_zcheck_safe_state();
      if ((arg->flags & INTAN_IMP_FLAG_RESTORE_REGS) != 0U) { (void)intan_restore_impedance_regs(saved); }
      return st;
    }
  }

  reg2 = (uint16_t)(((uint16_t)arg->channel << 8) | (1U << 6) |
                    ((uint16_t)arg->scale_bits << 3) | 1U);
  st = Intan_WriteReg(2U, 0x0040U, 0U, 0U);
  if (st == HAL_OK) { st = Intan_WriteReg(3U, 0x0080U, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(2U, reg2, 0U, 0U); }
  if (st == HAL_OK) { st = Intan_WriteReg(3U, 0x0080U, 0U, 0U); }
  if (st == HAL_OK)
  {
    HAL_Delay(20);
    st = intan_impedance_prepare_loop(arg->channel);
  }
  if (st != HAL_OK)
  {
    result->spi_errors++;
    (void)intan_zcheck_safe_state();
    if ((arg->flags & INTAN_IMP_FLAG_RESTORE_REGS) != 0U) { (void)intan_restore_impedance_regs(saved); }
    return st;
  }

  {
    int32_t period_sum_s = 0;
    int32_t period_sum_c = 0;

    intan_impedance_period_sums(arg->samples_per_period, &period_sum_s, &period_sum_c);
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    st = intan_impedance_run_paced_samples(arg, result, sample_count, slot_hz,
                                           period_sum_s, period_sum_c);
  }
  if (st != HAL_OK)
  {
    result->spi_errors++;
  }

  t_elapsed = result->elapsed_cycles;
  result->elapsed_cycles = t_elapsed;
  if (t_elapsed != 0U && result->sample_count != 0U)
  {
    if (result->actual_freq_millihz == 0U)
    {
      result->actual_freq_millihz =
          (uint32_t)(((uint64_t)result->sample_count * (uint64_t)SystemCoreClock * 1000ULL) /
                     ((uint64_t)t_elapsed * (uint64_t)arg->samples_per_period));
    }
  }

  if ((arg->flags & INTAN_IMP_FLAG_RESTORE_REGS) != 0U)
  {
    (void)intan_restore_impedance_regs(saved);
  }
  (void)intan_zcheck_safe_state();

  return (result->spi_errors == 0U) ? HAL_OK : HAL_ERROR;
}

uint32_t Intan_BuildConvertCmd(uint8_t channel, uint8_t flags)
{
  return intan_convert_cmd_word(channel, flags);
}

static uint8_t s_fw_spi_dma_active;

HAL_StatusTypeDef Intan_FwSpiDmaBegin(const uint32_t *tx_words, uint32_t *rx_words, uint32_t n_words)
{
  uint32_t old_midi;

  if (!g_intan_spi_ready || tx_words == NULL || rx_words == NULL || n_words == 0U)
  {
    return HAL_ERROR;
  }
  if (s_fw_spi_dma_active != 0U)
  {
    return HAL_BUSY;
  }

  if (intan_dma_timcs_ensure_armed(0U, 0U, &old_midi) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (intan_dma_prepare_streams_ex(tx_words, n_words, 1U, INTAN_DMA_STREAM_TX_REQUEST) != HAL_OK)
  {
    return HAL_TIMEOUT;
  }

  DMA1_Stream0->M0AR = (uint32_t)rx_words;

  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->CFG2 &= ~SPI_CFG2_COMM;
  MODIFY_REG(INTAN_SPI_INSTANCE->CFG2, SPI_CFG2_MIDI, Intan_StreamMidiHal());
  INTAN_SPI_INSTANCE->IER = 0U;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = n_words;
#if INTAN_CS_HW_NSS
  INTAN_SPI_INSTANCE->CFG1 |= SPI_CFG1_RXDMAEN | SPI_CFG1_TXDMAEN;
#else
  return HAL_ERROR;
#endif

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;
  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;
  s_fw_spi_dma_active = 1U;
  Intan_SpiStats_AddXfer32(n_words);
  return HAL_OK;
}

uint8_t Intan_FwSpiDmaPollDone(void)
{
  const uint32_t dma_stream0_done = DMA_LISR_TCIF0;
  const uint32_t dma_stream1_done = DMA_LISR_TCIF1;

  if (s_fw_spi_dma_active == 0U)
  {
    return 0U;
  }

  if ((DMA1->LISR & dma_stream0_done) == 0U)
  {
    return 0U;
  }
  if ((DMA1->LISR & dma_stream1_done) == 0U)
  {
    return 0U;
  }

  if ((INTAN_SPI_INSTANCE->SR & SPI_SR_EOT) == 0U)
  {
    return 0U;
  }

  return 1U;
}

uint8_t Intan_FwSpiDmaHasError(void)
{
  /*
   * UDR is defined by the H7 SPI as "underrun at slave transmission".
   * SPI2 is configured as a master here, so it is not a valid indication
   * that this full-duplex master DMA transaction has failed.  In particular,
   * checking it after CSTART made the phase-1 recovery path stop an otherwise
   * progressing RR8 stream.
   *
   * Keep this helper for diagnostics, but do not use it for RR8 hot-loop
   * recovery: H7 SPI master status around CSTART/NSS can produce transient
   * OVR/MODF indications despite a completed EOT transfer.  The hot loop
   * treats only a missing EOT at its DWT deadline as a recovery failure.
   */
  const uint32_t spi_errors = SPI_SR_OVR | SPI_SR_MODF;
  const uint32_t dma_errors = DMA_LISR_TEIF0 | DMA_LISR_DMEIF0 | DMA_LISR_FEIF0 |
                              DMA_LISR_TEIF1 | DMA_LISR_DMEIF1 | DMA_LISR_FEIF1;

  if (s_fw_spi_dma_active == 0U)
  {
    return 0U;
  }

  return (((INTAN_SPI_INSTANCE->SR & spi_errors) != 0U) ||
          ((DMA1->LISR & dma_errors) != 0U)) ? 1U : 0U;
}

HAL_StatusTypeDef Intan_FwSpiDmaRestart(uint32_t n_words)
{
  const uint32_t dma_stream0_flags = DMA_LIFCR_CFEIF0 | DMA_LIFCR_CDMEIF0 | DMA_LIFCR_CTEIF0 |
                                     DMA_LIFCR_CHTIF0 | DMA_LIFCR_CTCIF0;
  const uint32_t dma_stream1_flags = DMA_LIFCR_CFEIF1 | DMA_LIFCR_CDMEIF1 | DMA_LIFCR_CTEIF1 |
                                     DMA_LIFCR_CHTIF1 | DMA_LIFCR_CTCIF1;

  if (!g_intan_spi_ready || s_fw_spi_dma_active == 0U || n_words == 0U)
  {
    return HAL_ERROR;
  }

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  if (intan_wait_dma_stream_disabled(DMA1_Stream0) != HAL_OK ||
      intan_wait_dma_stream_disabled(DMA1_Stream1) != HAL_OK)
  {
    return HAL_TIMEOUT;
  }

  DMA1->LIFCR = dma_stream0_flags | dma_stream1_flags;
  DMA1_Stream0->NDTR = n_words;
  DMA1_Stream1->NDTR = n_words;

  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                             SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  INTAN_SPI_INSTANCE->CR2 = n_words;

  DMA1_Stream0->CR |= DMA_SxCR_EN;
  DMA1_Stream1->CR |= DMA_SxCR_EN;
  INTAN_SPI_INSTANCE->CR1 |= SPI_CR1_CSTART;
  Intan_SpiStats_AddXfer32(n_words);
  return HAL_OK;
}

void Intan_FwSpiDmaEnd(void)
{
  if (s_fw_spi_dma_active == 0U)
  {
    return;
  }

  DMA1_Stream0->CR &= ~DMA_SxCR_EN;
  DMA1_Stream1->CR &= ~DMA_SxCR_EN;
  (void)intan_wait_dma_stream_disabled(DMA1_Stream0);
  (void)intan_wait_dma_stream_disabled(DMA1_Stream1);
  INTAN_SPI_INSTANCE->CFG1 &= ~(SPI_CFG1_TXDMAEN | SPI_CFG1_RXDMAEN);
  INTAN_SPI_INSTANCE->CR1 &= ~SPI_CR1_SPE;
  INTAN_SPI_INSTANCE->IFCR = SPI_IFCR_EOTC | SPI_IFCR_TXTFC | SPI_IFCR_UDRC | SPI_IFCR_OVRC |
                               SPI_IFCR_MODFC | SPI_IFCR_SUSPC;
  s_fw_spi_dma_active = 0U;
}
