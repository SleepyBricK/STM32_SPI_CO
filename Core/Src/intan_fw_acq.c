/**
 * @file intan_fw_acq.c
 * @brief Intan Framework v1.2 acquisition path (8×CONVERT, 0 AUX) → IntanStream/USB.
 */

#include "intan_fw_acq.h"

#if (INTAN_HW_PRESENT == 1)

#include "intan_spi.h"
#include "intan_stream.h"
#include "usb_stream_frame.h"
#include "stm32h7xx_ll_bus.h"
#include "stm32h7xx_ll_tim.h"
#include <string.h>

#define INTAN_FW_TIM_IRQ_PRIO  2U
#define FW_USB_Q_DEPTH         32U
/* Eight 32-bit slots take about 11 us at the validated 25 MHz SCK; 1 ms is recovery only. */
#define FW_SPI_DMA_TIMEOUT_CYC (SystemCoreClock / 1000U)

typedef enum {
  FW_XFER_IDLE = 0,
  FW_XFER_WAIT,
} FwXferState;

static volatile uint8_t s_tick_pending;
static volatile uint8_t s_active;
static volatile uint8_t s_armed;
static volatile FwXferState s_xfer_state;
static volatile uint8_t s_usb_pending;
static volatile uint8_t s_stop_after_usb;

static uint32_t s_mosi[INTAN_FW_WORDS_PER_SEQ]
    __attribute__((section(".dma_buffer"), aligned(32)));
static uint32_t s_miso[INTAN_FW_WORDS_PER_SEQ]
    __attribute__((section(".dma_buffer"), aligned(32)));

static uint32_t s_remaining;
static uint32_t s_target_ksps;
static uint8_t s_channel;
static uint8_t s_flags;
static uint8_t s_all_channels;
static uint8_t s_freerun;
static uint8_t s_dwt_pace;
static uint32_t s_seq_interval_cyc;
static uint32_t s_seq_not_before_cyc;
static uint32_t s_xfer_start_cyc;
static uint32_t s_dma_error_count;
static uint32_t s_late_sequence_count;
static volatile uint8_t s_stop_requested;

static uint16_t s_usb_adc[INTAN_FW_CONVERT_SLOTS];
static uint16_t s_usb_q[FW_USB_Q_DEPTH][INTAN_FW_CONVERT_SLOTS];
static uint8_t s_usb_q_w;
static uint8_t s_usb_q_r;
static uint8_t s_usb_q_n;
static uint8_t s_saved_midi;
static uint8_t s_spi_tuned;
static uint32_t s_saved_pscl;

static void fw_tim6_hw_init(void);
static void fw_tim6_set_rate_hz(uint32_t hz);
static void fw_usb_q_reset(void)
{
  s_usb_q_w = 0U;
  s_usb_q_r = 0U;
  s_usb_q_n = 0U;
}

static uint8_t fw_usb_q_enqueue_all(const uint16_t *adc)
{
  if (s_usb_q_n >= FW_USB_Q_DEPTH)
  {
    Intan_BumpSampleClip();
    return 0U;
  }

  memcpy(s_usb_q[s_usb_q_w], adc, INTAN_FW_CONVERT_SLOTS * sizeof(uint16_t));
  s_usb_q_w = (uint8_t)((s_usb_q_w + 1U) % FW_USB_Q_DEPTH);
  s_usb_q_n++;
  s_usb_pending = 1U;
  return 1U;
}
static uint16_t fw_adc_from_channel(const uint32_t *miso, uint8_t channel);
static void fw_spi_dma_complete(void);
static void fw_usb_flush_pending(void);
static void fw_try_start_sequence(void);
static HAL_StatusTypeDef fw_pipeline_warmup(void);
static HAL_StatusTypeDef fw_begin_acquisition(void);
static void fw_apply_rate_tuning(uint32_t target_ch_ksps);
static void fw_restore_rate_tuning(void);
static void fw_dwt_pace_before_start(void);

static void fw_abort_dma_failure(void)
{
  s_dma_error_count++;
  Intan_FwSpiDmaEnd();
  s_xfer_state = FW_XFER_IDLE;
  Intan_DmaPathRelease();
  IntanFw_StreamStop();
}

static uint32_t fw_tim6_input_hz(void)
{
  uint32_t pclk1 = HAL_RCC_GetPCLK1Freq();
  uint32_t presc = (RCC->D2CFGR & RCC_D2CFGR_D2PPRE1) >> RCC_D2CFGR_D2PPRE1_Pos;

  if (presc >= 4U)
  {
    return pclk1 * 2U;
  }

  return pclk1;
}

static void fw_tim6_hw_init(void)
{
  LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_TIM6);
  NVIC_SetPriority(TIM6_DAC_IRQn, INTAN_FW_TIM_IRQ_PRIO);
  NVIC_EnableIRQ(TIM6_DAC_IRQn);

  LL_TIM_SetPrescaler(TIM6, 0U);
  LL_TIM_SetCounterMode(TIM6, LL_TIM_COUNTERMODE_UP);
  LL_TIM_EnableARRPreload(TIM6);
  LL_TIM_SetCounter(TIM6, 0U);
  LL_TIM_ClearFlag_UPDATE(TIM6);
  LL_TIM_EnableIT_UPDATE(TIM6);
}

static void fw_tim6_set_rate_hz(uint32_t hz)
{
  uint32_t tim_hz = fw_tim6_input_hz();
  uint32_t arr;

  if (hz == 0U)
  {
    hz = INTAN_FW_KSPS_DEFAULT * 1000U;
  }
  if (hz < 100U)
  {
    hz = 100U;
  }

  arr = tim_hz / hz;
  if (arr < 2U)
  {
    arr = 2U;
  }
  arr -= 1U;

  LL_TIM_DisableCounter(TIM6);
  LL_TIM_SetAutoReload(TIM6, arr);
  LL_TIM_SetCounter(TIM6, 0U);
  LL_TIM_ClearFlag_UPDATE(TIM6);
}

static void fw_build_mosi_sequence(void)
{
  uint32_t i;

  for (i = 0U; i < INTAN_FW_CONVERT_SLOTS; i++)
  {
    uint8_t ch = (uint8_t)i;
    uint8_t flags = 0U;

    if (s_all_channels == 0U && ch == s_channel)
    {
      flags = s_flags;
    }

    s_mosi[i] = Intan_BuildConvertCmd(ch, flags);
  }
}

static uint16_t fw_adc_from_channel(const uint32_t *miso, uint8_t channel)
{
  uint32_t w;
  /* Pipeline +2; last two channels wrap to MISO[0,1] (RR16: ch14/15, RR8: ch6/7). */
  const uint8_t wrap_base = (uint8_t)(INTAN_FW_CONVERT_SLOTS - 2U);

  if (channel < wrap_base)
  {
    w = miso[channel + 2U];
  }
  else
  {
    w = miso[(uint8_t)(channel - wrap_base)];
  }

  return (uint16_t)((w >> 16) & 0xFFFFU);
}

static void fw_unpack_all_channels(const uint32_t *miso, uint16_t *adc_out)
{
  uint8_t ch;

  for (ch = 0U; ch < INTAN_FW_CONVERT_SLOTS; ch++)
  {
    adc_out[ch] = fw_adc_from_channel(miso, ch);
  }
}

static HAL_StatusTypeDef fw_spi_wait_done(uint32_t timeout_cyc)
{
  uint32_t t0 = DWT->CYCCNT;

  while (Intan_FwSpiDmaPollDone() == 0U)
  {
    if ((DWT->CYCCNT - t0) > timeout_cyc)
    {
      Intan_FwSpiDmaEnd();
      return HAL_TIMEOUT;
    }
  }

  return HAL_OK;
}

static void fw_apply_rate_tuning(uint32_t target_ch_ksps)
{
  s_spi_tuned = 0U;

  if (s_freerun != 0U || s_all_channels == 0U)
  {
    return;
  }

  if (target_ch_ksps >= INTAN_FW_KSPS_HIGH_RATE)
  {
    s_saved_midi = Intan_GetStreamMidiCycles();
    Intan_SetStreamMidiCycles(INTAN_FW_KSPS_HIGH_MIDI);
    s_spi_tuned = 1U;
  }

  if (target_ch_ksps >= INTAN_FW_KSPS_FAST_SPI)
  {
    s_saved_pscl = Intan_GetSpiPrescalerDiv();
    if (Intan_SetSpiPrescalerDiv(INTAN_FW_KSPS_FAST_PSCL) == HAL_OK)
    {
      s_spi_tuned = 1U;
    }
  }
}

static void fw_restore_rate_tuning(void)
{
  if (s_spi_tuned != 0U)
  {
    Intan_SetStreamMidiCycles(s_saved_midi);
    if (s_saved_pscl != 0U)
    {
      (void)Intan_SetSpiPrescalerDiv(s_saved_pscl);
      s_saved_pscl = 0U;
    }
    s_spi_tuned = 0U;
  }
}

/** Wait for next phase tick, then advance (interval from sequence start, not SPI end). */
static void fw_dwt_pace_before_start(void)
{
  uint32_t now;

  if (s_dwt_pace == 0U)
  {
    return;
  }

  now = DWT->CYCCNT;
  if ((int32_t)(now - s_seq_not_before_cyc) >= (int32_t)s_seq_interval_cyc)
  {
    /* ≥1 slot late: resync phase to avoid compounding idle after long SPI. */
    s_late_sequence_count += (now - s_seq_not_before_cyc) / s_seq_interval_cyc;
    s_seq_not_before_cyc = now;
  }

  while (s_active != 0U && (int32_t)(DWT->CYCCNT - s_seq_not_before_cyc) < 0)
  {
    fw_usb_flush_pending();
  }

  s_seq_not_before_cyc += s_seq_interval_cyc;
}

static HAL_StatusTypeDef fw_pipeline_warmup(void)
{
  uint32_t pass;
  const uint32_t timeout_cyc = SystemCoreClock / 10U;

  for (pass = 0U; pass < 3U; pass++)
  {
    if (Intan_FwSpiDmaBegin(s_mosi, s_miso, INTAN_FW_WORDS_PER_SEQ) != HAL_OK)
    {
      return HAL_ERROR;
    }

    if (fw_spi_wait_done(timeout_cyc) != HAL_OK)
    {
      return HAL_TIMEOUT;
    }

    Intan_FwSpiDmaEnd();
  }

  return HAL_OK;
}

static void fw_spi_dma_complete(void)
{
  uint8_t keep_live;
  uint8_t enqueue_ok = 1U;

  if (s_all_channels != 0U)
  {
    fw_unpack_all_channels(s_miso, s_usb_adc);
    if (fw_usb_q_enqueue_all(s_usb_adc) == 0U)
    {
      enqueue_ok = 0U;
    }
  }
  else
  {
    s_usb_adc[0] = fw_adc_from_channel(s_miso, s_channel);
    s_usb_pending = 1U;
  }

  s_remaining--;

  if (s_remaining == 0U)
  {
    s_stop_after_usb = 1U;
    if (s_freerun == 0U && s_dwt_pace == 0U)
    {
      LL_TIM_DisableCounter(TIM6);
    }
  }

  keep_live = ((s_freerun != 0U || s_dwt_pace != 0U) && s_remaining > 0U && s_active != 0U &&
               enqueue_ok != 0U && s_stop_requested == 0U)
                  ? 1U
                  : 0U;
  if (keep_live != 0U && s_all_channels != 0U && s_usb_q_n >= FW_USB_Q_DEPTH)
  {
    keep_live = 0U;
  }

  if (keep_live != 0U)
  {
    fw_dwt_pace_before_start();

    if (Intan_FwSpiDmaRestart(INTAN_FW_WORDS_PER_SEQ) == HAL_OK)
    {
      s_xfer_start_cyc = DWT->CYCCNT;
      s_xfer_state = FW_XFER_WAIT;
      return;
    }

    Intan_BumpSampleClip();
  }

  Intan_FwSpiDmaEnd();
  s_xfer_state = FW_XFER_IDLE;
  fw_try_start_sequence();
}

static void fw_usb_flush_pending(void)
{
  if (s_usb_pending == 0U || IntanStream_IsActive() == 0U)
  {
    return;
  }

  if (s_all_channels != 0U)
  {
    while (s_usb_q_n > 0U)
    {
      IntanStream_PushBlock(s_usb_q[s_usb_q_r], INTAN_FW_CONVERT_SLOTS);
      s_usb_q_r = (uint8_t)((s_usb_q_r + 1U) % FW_USB_Q_DEPTH);
      s_usb_q_n--;
    }

    s_usb_pending = 0U;
  }
  else
  {
    IntanStream_PushResponse(s_usb_adc[0]);
    s_usb_pending = 0U;
  }

  if (s_stop_after_usb != 0U)
  {
    s_stop_after_usb = 0U;
    IntanFw_StreamStop();
  }
}

static void fw_try_start_sequence(void)
{
  if (s_active == 0U || s_xfer_state == FW_XFER_WAIT || s_stop_requested != 0U)
  {
    return;
  }

  if (s_freerun == 0U && s_dwt_pace == 0U)
  {
    if (s_tick_pending == 0U)
    {
      return;
    }

    s_tick_pending = 0U;
  }
  else if (s_remaining == 0U)
  {
    return;
  }

  fw_dwt_pace_before_start();

  if ((s_freerun != 0U || s_dwt_pace != 0U) && s_all_channels != 0U && s_usb_q_n >= FW_USB_Q_DEPTH)
  {
    return;
  }

  if (Intan_FwSpiDmaBegin(s_mosi, s_miso, INTAN_FW_WORDS_PER_SEQ) != HAL_OK)
  {
    Intan_BumpSampleClip();
    return;
  }

  s_xfer_start_cyc = DWT->CYCCNT;
  s_xfer_state = FW_XFER_WAIT;
}

void TIM6_DAC_IRQHandler(void)
{
  if (LL_TIM_IsActiveFlag_UPDATE(TIM6) != 0U)
  {
    LL_TIM_ClearFlag_UPDATE(TIM6);
    IntanFw_OnTimerTick();
  }
}

void IntanFw_OnTimerTick(void)
{
  if (s_active == 0U || s_freerun != 0U || s_dwt_pace != 0U)
  {
    return;
  }

  if (s_xfer_state == FW_XFER_WAIT)
  {
    Intan_BumpSampleClip();
  }

  s_tick_pending = 1U;
}

void IntanFw_Process(void)
{
  uint32_t pass;

  if (s_stop_requested != 0U && s_xfer_state == FW_XFER_IDLE)
  {
    IntanFw_StreamStop();
    return;
  }

  if (s_armed != 0U)
  {
    s_armed = 0U;
    if (fw_begin_acquisition() != HAL_OK)
    {
      IntanFw_StreamStop();
      return;
    }
  }

  if (s_active == 0U)
  {
    return;
  }

  for (pass = 0U; pass < ((s_freerun != 0U || s_dwt_pace != 0U) ? 256U : 64U); pass++)
  {
    fw_usb_flush_pending();

    if (s_xfer_state == FW_XFER_WAIT)
    {
      /*
       * The H7 SPI status flags can be asserted during a valid master NSS
       * sequence.  The RR8 hot path therefore recovers only on a missing EOT
       * after the 1 ms DWT deadline; Intan_FwSpiDmaHasError() is diagnostic.
       */
      if (Intan_FwSpiDmaPollDone() != 0U)
      {
        fw_spi_dma_complete();
      }
      else if ((DWT->CYCCNT - s_xfer_start_cyc) > FW_SPI_DMA_TIMEOUT_CYC)
      {
        fw_abort_dma_failure();
        return;
      }
    }

    /* STOP is consumed only after the in-flight sequence has reached EOT. */
    if (s_stop_requested != 0U && s_xfer_state == FW_XFER_IDLE)
    {
      IntanFw_StreamStop();
      return;
    }

    fw_try_start_sequence();

    if (s_active == 0U)
    {
      break;
    }

    if (s_xfer_state == FW_XFER_WAIT)
    {
      continue;
    }

    if (s_usb_pending != 0U)
    {
      continue;
    }

    if (s_freerun != 0U || s_dwt_pace != 0U)
    {
      if (s_remaining > 0U)
      {
        continue;
      }
      break;
    }

    if (s_tick_pending == 0U)
    {
      break;
    }
  }
}

static HAL_StatusTypeDef fw_begin_acquisition(void)
{
  Intan_StreamDmaReset();
  Intan_DmaPathRelease();

  if (fw_pipeline_warmup() != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (s_all_channels != 0U)
  {
    /* Untagged ch0..15 block: half the USB bytes vs tagged. */
    IntanStream_BeginWithMeta(USB_STREAM_FLAG_REAL_ADC,
                              USB_STREAM_META(0U, INTAN_FW_CONVERT_SLOTS, s_flags, 0U));
  }
  else
  {
    IntanStream_Begin();
  }

  fw_tim6_hw_init();
  if (s_freerun == 0U && s_dwt_pace == 0U)
  {
    fw_tim6_set_rate_hz(s_target_ksps * 1000U);
  }

  s_xfer_state = FW_XFER_IDLE;
  s_tick_pending = 0U;
  s_usb_pending = 0U;
  fw_usb_q_reset();
  s_active = 1U;

  if (s_freerun != 0U || s_dwt_pace != 0U)
  {
    if (s_dwt_pace != 0U)
    {
      s_seq_not_before_cyc = DWT->CYCCNT;
    }

    fw_try_start_sequence();
  }
  else
  {
    LL_TIM_EnableCounter(TIM6);
  }

  return HAL_OK;
}

HAL_StatusTypeDef IntanFw_StreamStart(uint32_t n, uint8_t channel, uint8_t flags, uint32_t target_ch_ksps)
{
  if (Intan_SPI_IsReady() == 0U || n == 0U)
  {
    return HAL_ERROR;
  }

  if (channel == INTAN_FW_CHANNEL_ALL)
  {
    s_all_channels = 1U;
    s_channel = 0U;
  }
  else if (channel >= INTAN_FW_CONVERT_SLOTS)
  {
    return HAL_ERROR;
  }
  else
  {
    s_all_channels = 0U;
    s_channel = channel;
  }

  IntanFw_StreamStop();
  Intan_SpiStats_Reset();

  if (target_ch_ksps == 0U)
  {
    target_ch_ksps = INTAN_FW_KSPS_DEFAULT;
  }

  s_flags = flags;
  s_remaining = n;
  s_target_ksps = target_ch_ksps;
  s_freerun = (target_ch_ksps == INTAN_FW_KSPS_FREERUN) ? 1U : 0U;
  s_dwt_pace = 0U;
  s_stop_requested = 0U;
  s_late_sequence_count = 0U;

  if (s_all_channels != 0U && target_ch_ksps == INTAN_FW_KSPS_DEFAULT)
  {
    /* Validated production RR8 is fixed; diagnostic rates retain fw_apply_rate_tuning(). */
    if (Intan_SetSpiPrescalerDiv(8U) != HAL_OK)
    {
      return HAL_ERROR;
    }
    Intan_SetStreamMidiCycles(4U);
  }
  if (s_freerun == 0U && s_all_channels != 0U && target_ch_ksps >= INTAN_FW_KSPS_DWT_PACE_MIN)
  {
    uint64_t hz = (uint64_t)target_ch_ksps * 1000ULL;

    s_dwt_pace = 1U;
    s_seq_interval_cyc = (uint32_t)((uint64_t)SystemCoreClock / hz);
    if (s_seq_interval_cyc < 100U)
    {
      s_seq_interval_cyc = 100U;
    }
  }

  fw_apply_rate_tuning(target_ch_ksps);
  fw_build_mosi_sequence();
  s_armed = 1U;

  return HAL_OK;
}

void IntanFw_RequestStop(void)
{
  s_stop_requested = 1U;
}

void IntanFw_StreamStop(void)
{
  fw_restore_rate_tuning();
  s_armed = 0U;
  s_freerun = 0U;
  s_dwt_pace = 0U;
  s_stop_after_usb = 0U;
  s_stop_requested = 0U;
  LL_TIM_DisableCounter(TIM6);
  LL_TIM_DisableIT_UPDATE(TIM6);
  s_active = 0U;
  s_tick_pending = 0U;
  s_usb_pending = 0U;
  fw_usb_q_reset();

  if (s_xfer_state == FW_XFER_WAIT)
  {
    Intan_FwSpiDmaEnd();
    s_xfer_state = FW_XFER_IDLE;
  }

  if (IntanStream_IsActive() != 0U)
  {
    IntanStream_End();
  }

  Intan_FwSpiDmaEnd();
  Intan_DmaPathRelease();
}

uint8_t IntanFw_StreamIsActive(void)
{
  return s_active;
}

uint8_t IntanFw_StreamIsBusy(void)
{
  return (s_active != 0U || s_armed != 0U) ? 1U : 0U;
}

uint8_t IntanFw_StreamIsFreerun(void)
{
  return s_freerun;
}

uint8_t IntanFw_StreamUsesHotLoop(void)
{
  return (s_freerun != 0U || s_dwt_pace != 0U) ? 1U : 0U;
}

uint32_t IntanFw_GetSampleClipCount(void)
{
  return Intan_GetSampleClipCount();
}

uint32_t IntanFw_GetDmaErrorCount(void)
{
  return s_dma_error_count;
}

uint32_t IntanFw_GetLateSequenceCount(void)
{
  return s_late_sequence_count;
}

#else /* INTAN_HW_PRESENT */

void TIM6_DAC_IRQHandler(void)
{
}

void IntanFw_OnTimerTick(void)
{
}

void IntanFw_Process(void)
{
}

HAL_StatusTypeDef IntanFw_StreamStart(uint32_t n, uint8_t channel, uint8_t flags, uint32_t target_ch_ksps)
{
  (void)n;
  (void)channel;
  (void)flags;
  (void)target_ch_ksps;
  return HAL_ERROR;
}

void IntanFw_RequestStop(void)
{
}

void IntanFw_StreamStop(void)
{
}

uint8_t IntanFw_StreamIsActive(void)
{
  return 0U;
}

uint8_t IntanFw_StreamIsBusy(void)
{
  return 0U;
}

uint8_t IntanFw_StreamIsFreerun(void)
{
  return 0U;
}

uint8_t IntanFw_StreamUsesHotLoop(void)
{
  return 0U;
}

uint32_t IntanFw_GetSampleClipCount(void)
{
  return 0U;
}

uint32_t IntanFw_GetDmaErrorCount(void)
{
  return 0U;
}

uint32_t IntanFw_GetLateSequenceCount(void)
{
  return 0U;
}

#endif /* INTAN_HW_PRESENT */
