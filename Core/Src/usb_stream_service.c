#include "usb_stream_service.h"
#include "usb_commands.h"
#include "usb_stream_ring.h"
#include "usb_stream_frame.h"
#include "usb_vendor_bulk.h"
#include "intan_stream.h"
#include "intan_spi.h"
#include "intan_spi_diag.h"
#include "intan_app.h"
#include "intan_pattern.h"
#include <stdio.h>
#include <string.h>

#define USB_CMD_RX_MAX        256U
#define USB_REPLY_MAX         512U
#define SPI_STREAM_CHUNK_MAX  (INTAN_DMA_CHUNK_SLOTS - 2U)
#define SPI_STREAM_SAFE_CHUNK_MAX  128U
#define SPI_CHUNKS_PER_TICK   8U
#define INTAN_RECORD_STREAM_ADC_KSPS INTAN_APP_RECORD_ADC_KSPS
#define SPI_REAL_PATH_DMA_TIMCS       0U
#define SPI_REAL_PATH_SAFE_POLLING    1U
#define SPI_REAL_PATH_FAST_POLLING    2U
#define SPI_REAL_PATH_DMA_TIMSLOT     3U

static UsbStreamStats s_stats;
static uint8_t s_cmd_rx[USB_CMD_RX_MAX];
static char s_reply[USB_REPLY_MAX];

static uint8_t s_synth_active;
static uint32_t s_synth_remaining;
static uint32_t s_next_sample;
static uint32_t s_next_frame_seq;

static uint8_t s_spi_active;
static uint32_t s_spi_remaining;
static uint8_t s_spi_channel;
static uint8_t s_spi_flags;
static uint8_t s_spi_counter_mode;
static uint8_t s_spi_usb_pump;
static uint8_t s_spi_rr_first;
static uint8_t s_spi_rr_channels;
static uint8_t s_spi_rr_phase;
static uint8_t s_spi_safe_polling;

static uint16_t s_spi_buf[SPI_STREAM_CHUNK_MAX]
    __attribute__((section(".dma_buffer"), aligned(32)));

static UsbStreamFrame *s_tx_frame;
static uint8_t s_tx_active;

static void usb_stream_on_frame_tx_complete(uint32_t len);
static void usb_stream_tx_pump(void);
static void usb_reply_text(const char *text);
static void usb_stream_reset_all(void);
static void usb_spi_stream_process(void);
static uint8_t usb_spi_run_one_chunk(void);
static void usb_cmd_spi_rate(uint32_t n, uint8_t channel, uint8_t flags);
static void usb_cmd_spi_rate_fast(uint32_t n, uint8_t channel, uint8_t flags);
static void usb_cmd_spi_to_ram(uint32_t n, uint8_t channel, uint8_t flags);
static void usb_cmd_spi_rate_rr8(uint32_t n, uint8_t flags);
static void usb_cmd_spi_to_ram_rr8(uint32_t n, uint8_t flags);
static void usb_intan_stop_stream(void);
static void usb_cmd_id(void);
static void usb_cmd_read(uint32_t reg);
static void usb_cmd_write(uint32_t reg, uint32_t value, uint32_t u, uint32_t m);
static void usb_cmd_init_record(uint32_t ksps);
static void usb_cmd_init_stim(void);
static void usb_cmd_clear_adc(void);
static void usb_cmd_clear_comp(void);
static void usb_cmd_convert(uint32_t channel, uint32_t flags);
static void usb_cmd_pattern_status(void);
static void usb_cmd_pattern_run(uint32_t repeat);
static void usb_stats_reply(void);
static uint32_t usb_stream_rate_ksps_x10(uint32_t samples, uint32_t elapsed_ms);

static uint32_t usb_stream_rate_ksps_x10(uint32_t samples, uint32_t elapsed_ms)
{
  if (elapsed_ms == 0U)
  {
    return 0U;
  }

  return (uint32_t)(((uint64_t)samples * 10ULL) / (uint64_t)elapsed_ms);
}

static void usb_stream_on_frame_tx_complete(uint32_t len)
{
  (void)len;

  if (s_tx_frame != NULL)
  {
    UsbStreamRing_MarkFree(s_tx_frame);
    s_tx_frame = NULL;
  }

  s_tx_active = 0U;
  s_stats.frames_sent++;
  usb_stream_tx_pump();
}

static void usb_stream_tx_pump(void)
{
  UsbStreamFrame *frame;
  uint8_t st;

  if (s_tx_active != 0U)
  {
    return;
  }

  frame = UsbStreamRing_PopReady();
  if (frame == NULL)
  {
    return;
  }

  UsbStreamRing_MarkTxBusy(frame);

  st = USBD_VENDOR_BULK_TransmitFrame((const uint8_t *)frame, USB_STREAM_FRAME_SIZE);
  if (st != (uint8_t)USBD_OK)
  {
    if (UsbStreamRing_PushReadyFront(frame) == 0U)
    {
      UsbStreamRing_MarkFree(frame);
      s_stats.usb_overflow_count++;
    }
    s_stats.usb_tx_errors++;
    return;
  }

  s_tx_frame = frame;
  s_tx_active = 1U;
}

static void usb_reply_text(const char *text)
{
  int n;

  if (text == NULL)
  {
    return;
  }

  n = snprintf(s_reply, sizeof(s_reply), "%s\n", text);
  if (n <= 0)
  {
    return;
  }
  if ((size_t)n >= sizeof(s_reply))
  {
    n = (int)sizeof(s_reply) - 1;
    s_reply[n] = '\0';
  }

  (void)USBD_VENDOR_BULK_Transmit((uint8_t *)s_reply, (uint16_t)n);
}

static size_t usb_append_str(char *dst, size_t cap, size_t pos, const char *src)
{
  if (dst == NULL || src == NULL || cap == 0U || pos >= cap)
  {
    return pos;
  }
  while (*src != '\0' && (pos + 1U) < cap)
  {
    dst[pos++] = *src++;
  }
  dst[pos] = '\0';
  return pos;
}

static size_t usb_append_u32(char *dst, size_t cap, size_t pos, uint32_t value)
{
  char tmp[11];
  unsigned int i = 0U;

  if (value == 0U)
  {
    return usb_append_str(dst, cap, pos, "0");
  }
  while (value != 0U && i < sizeof(tmp))
  {
    tmp[i++] = (char)('0' + (value % 10U));
    value /= 10U;
  }
  while (i > 0U && (pos + 1U) < cap)
  {
    dst[pos++] = tmp[--i];
  }
  if (dst != NULL && cap != 0U && pos < cap)
  {
    dst[pos] = '\0';
  }
  return pos;
}

static size_t usb_append_i64(char *dst, size_t cap, size_t pos, int64_t value)
{
  char tmp[20];
  uint64_t mag;
  unsigned int i = 0U;

  if (value < 0)
  {
    pos = usb_append_str(dst, cap, pos, "-");
    mag = (uint64_t)(-(value + 1)) + 1ULL;
  }
  else
  {
    mag = (uint64_t)value;
  }
  if (mag == 0ULL)
  {
    return usb_append_str(dst, cap, pos, "0");
  }
  while (mag != 0ULL && i < sizeof(tmp))
  {
    tmp[i++] = (char)('0' + (mag % 10ULL));
    mag /= 10ULL;
  }
  while (i > 0U && (pos + 1U) < cap)
  {
    dst[pos++] = tmp[--i];
  }
  if (dst != NULL && cap != 0U && pos < cap)
  {
    dst[pos] = '\0';
  }
  return pos;
}

static void usb_stream_reset_all(void)
{
  s_synth_active = 0U;
  s_synth_remaining = 0U;
  s_spi_active = 0U;
  s_spi_remaining = 0U;
  s_spi_rr_first = 0U;
  s_spi_rr_channels = 0U;
  s_spi_rr_phase = 0U;
  s_spi_safe_polling = SPI_REAL_PATH_DMA_TIMCS;
  s_tx_active = 0U;
  s_tx_frame = NULL;
  IntanStream_Reset();
  UsbStreamRing_Reset();
  Intan_DmaPathRelease();
}

static void usb_spi_stream_start(uint32_t n, uint8_t channel, uint8_t flags, uint8_t counter_mode,
                                 uint8_t usb_pump, uint8_t rr_first, uint8_t rr_channels)
{
  uint16_t frame_flags = 0U;
  uint8_t first_channel = channel;
  uint8_t channel_count = 1U;
  uint8_t channel_bits = 0U;
  uint32_t stream_meta;

  usb_stream_reset_all();
  memset(&s_stats, 0, sizeof(s_stats));
  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();

  s_spi_remaining = n;
  s_spi_channel = channel;
  s_spi_flags = flags;
  s_spi_counter_mode = counter_mode;
  s_spi_usb_pump = usb_pump;
  s_spi_rr_first = rr_first;
  s_spi_rr_channels = rr_channels;
  s_spi_rr_phase = 0U;
  s_spi_safe_polling = (counter_mode == 0U) ? SPI_REAL_PATH_DMA_TIMSLOT : SPI_REAL_PATH_DMA_TIMCS;
  s_spi_active = (n > 0U) ? 1U : 0U;

  if (counter_mode != 0U)
  {
    frame_flags |= USB_STREAM_FLAG_COUNTER;
  }
  else
  {
    frame_flags |= USB_STREAM_FLAG_REAL_ADC;
  }
  if (rr_channels != 0U)
  {
    frame_flags |= USB_STREAM_FLAG_RR;
    first_channel = rr_first;
    channel_count = rr_channels;
  }
  else if (channel == 63U)
  {
    first_channel = 0U;
    channel_count = 16U;
  }

  if (counter_mode == 0U && channel_count > 1U)
  {
    frame_flags |= USB_STREAM_FLAG_CHANNEL_TAG;
    channel_bits = UsbStream_ChannelBitsForCount(channel_count);
  }

  stream_meta = USB_STREAM_META(first_channel, channel_count, flags, channel_bits);
  Intan_SetDmaStreamContinuous(1U);

  IntanStream_BeginWithMeta(frame_flags, stream_meta);
}

static uint8_t usb_spi_run_one_chunk(void)
{
  uint32_t chunk;
  uint8_t phase0;
  HAL_StatusTypeDef st;

  if (s_spi_remaining == 0U)
  {
    return 0U;
  }

  chunk = s_spi_remaining;
  if (chunk > SPI_STREAM_CHUNK_MAX)
  {
    chunk = SPI_STREAM_CHUNK_MAX;
  }
  if ((s_spi_safe_polling == SPI_REAL_PATH_SAFE_POLLING) && (chunk > SPI_STREAM_SAFE_CHUNK_MAX))
  {
    chunk = SPI_STREAM_SAFE_CHUNK_MAX;
  }

  phase0 = s_spi_rr_phase;
  if (s_spi_rr_channels != 0U)
  {
    if (s_spi_safe_polling == SPI_REAL_PATH_DMA_TIMSLOT)
    {
      st = Intan_ConvertPipelineDmaTimSlotReadRange(chunk, s_spi_rr_first, s_spi_rr_channels,
                                                    s_spi_flags, s_spi_buf, &s_spi_rr_phase);
    }
    else if ((s_spi_safe_polling != SPI_REAL_PATH_DMA_TIMCS) && (s_spi_rr_first == 0U))
    {
      st = Intan_ConvertPipelineSafeReadRR(chunk, s_spi_rr_channels, s_spi_flags,
                                           s_spi_buf, &s_spi_rr_phase);
    }
    else if (s_spi_rr_first == 0U)
    {
      st = Intan_ConvertPipelineDmaTimCsReadRR(chunk, s_spi_rr_channels, s_spi_flags,
                                               s_spi_buf, &s_spi_rr_phase);
    }
    else
    {
      st = HAL_ERROR;
    }
  }
  else
  {
    if (s_spi_safe_polling == SPI_REAL_PATH_SAFE_POLLING)
    {
      st = Intan_ConvertPipelineSafeRead(chunk, s_spi_channel, s_spi_flags, s_spi_buf);
    }
    else if (s_spi_safe_polling == SPI_REAL_PATH_FAST_POLLING)
    {
      st = Intan_ConvertPipelineRead(chunk, s_spi_channel, s_spi_flags, s_spi_buf);
    }
    else if (s_spi_safe_polling == SPI_REAL_PATH_DMA_TIMSLOT)
    {
      st = Intan_ConvertPipelineDmaTimSlotRead(chunk, s_spi_channel, s_spi_flags, s_spi_buf);
    }
    else
    {
      st = Intan_ConvertPipelineDmaTimCsRead(chunk, s_spi_channel, s_spi_flags, s_spi_buf);
    }
  }
  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();

  if (st != HAL_OK)
  {
    s_stats.spi_overflow_count += chunk;
    s_spi_remaining = 0U;
    return 0U;
  }

  if (s_spi_counter_mode != 0U)
  {
    IntanStream_PushCounterBlock(IntanStream_PeekNextSample(), chunk);
  }
  else if (s_spi_rr_channels > 1U)
  {
    IntanStream_PushBlockTaggedFromAdc(s_spi_buf, chunk, s_spi_rr_first, s_spi_rr_channels, phase0);
  }
  else
  {
    IntanStream_PushBlock(s_spi_buf, chunk);
  }

  s_spi_remaining -= chunk;
  return (s_spi_remaining > 0U) ? 1U : 0U;
}

static void usb_spi_stream_process(void)
{
  uint32_t n;

  if (s_spi_active == 0U)
  {
    return;
  }

#if (INTAN_HW_PRESENT == 0)
  s_spi_active = 0U;
  IntanStream_End();
  return;
#else
  if (Intan_SPI_IsReady() == 0U)
  {
    s_stats.spi_overflow_count += s_spi_remaining;
    s_spi_active = 0U;
    IntanStream_End();
    Intan_DmaPathRelease();
    return;
  }

  for (n = 0U; n < SPI_CHUNKS_PER_TICK; n++)
  {
    if (s_spi_active == 0U)
    {
      break;
    }

    if (UsbStreamRing_FreeCount() < 2U)
    {
      break;
    }

    if (usb_spi_run_one_chunk() == 0U)
    {
      s_spi_active = 0U;
      IntanStream_End();
      Intan_DmaPathRelease();
      break;
    }

    if (s_spi_usb_pump != 0U)
    {
      UsbStreamService_TxPump();
    }
  }
#endif
}

static void usb_stats_reply(void)
{
  char stats_line[384];
  IntanSpiDiagSnapshot clk;
  uint32_t xfer_per_resp_x1000 = 0U;
  uint32_t ksps_from_cyc_x10 = 0U;
  uint32_t wall_ksps_x10 = 0U;

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  s_stats.responses_pushed = s_stats.samples_produced;

  if (s_stats.responses_pushed > 0U)
  {
    xfer_per_resp_x1000 =
        (s_stats.spi_xfer32_count * 1000U) / s_stats.responses_pushed;
  }

  Intan_SpiDiag_ReadClockConfig(&clk);
  if (clk.sample_period_avg_cycles > 0U)
  {
    ksps_from_cyc_x10 = Intan_SpiDiag_KspsFromCycX10(clk.sample_period_avg_cycles);
  }
  if (clk.wall_cyc_per_sample > 0U)
  {
    wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
  }

  (void)snprintf(stats_line, sizeof(stats_line),
                 "samples=%lu frames_out=%lu spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                 "usb_ovf=%lu spi_ovf=%lu tx_err=%lu "
                 "sysclk_mhz=%lu spi_khz=%lu sck_khz=%lu pscl=%lu tim_p=%lu "
                 "cyc_samp=%lu ksps_cyc_x10=%lu wall_cyc=%lu wall_ksps_x10=%lu",
                 (unsigned long)s_stats.samples_produced,
                 (unsigned long)s_stats.frames_sent,
                 (unsigned long)s_stats.spi_xfer32_count,
                 (unsigned long)xfer_per_resp_x1000,
                 (unsigned long)s_stats.usb_overflow_count,
                 (unsigned long)s_stats.spi_overflow_count,
                 (unsigned long)s_stats.usb_tx_errors,
                 (unsigned long)(SystemCoreClock / 1000000U),
                 (unsigned long)(clk.spi_kernel_hz / 1000U),
                 (unsigned long)(clk.spi_sck_hz_calc / 1000U),
                 (unsigned long)clk.spi_prescaler_div,
                 (unsigned long)clk.tim_period_ticks,
                 (unsigned long)clk.sample_period_avg_cycles,
                 (unsigned long)ksps_from_cyc_x10,
                 (unsigned long)clk.wall_cyc_per_sample,
                 (unsigned long)wall_ksps_x10);
  usb_reply_text(stats_line);
}

#if (INTAN_HW_PRESENT == 1)
static HAL_StatusTypeDef usb_reset_record_hpf(uint8_t first_ch, uint8_t n_ch, uint8_t flags)
{
  HAL_StatusTypeDef st;
  uint16_t dummy;
  uint8_t reset_flags = (uint8_t)(flags | 1U);

  if (n_ch > 1U)
  {
    for (uint8_t ch = 0U; ch < n_ch; ch++)
    {
      st = Intan_Convert((uint8_t)(first_ch + ch), reset_flags, &dummy);
      if (st != HAL_OK)
      {
        return st;
      }
    }
  }
  else
  {
    st = Intan_Convert(first_ch, reset_flags, &dummy);
    if (st != HAL_OK)
    {
      return st;
    }
  }

  HAL_Delay(1U);
  return HAL_OK;
}

static HAL_StatusTypeDef usb_prepare_real_record_stream(uint8_t first_ch, uint8_t n_ch, uint8_t flags)
{
  HAL_StatusTypeDef st;

  if (Intan_SPI_IsReady() == 0U)
  {
    return HAL_ERROR;
  }

  usb_intan_stop_stream();
  st = Intan_App_InitRecord(INTAN_RECORD_STREAM_ADC_KSPS);
  if (st != HAL_OK)
  {
    return st;
  }

  return usb_reset_record_hpf(first_ch, n_ch, flags);
}

static void usb_cmd_spi_rate(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t remaining = n;
  uint32_t total = 0U;
  uint32_t ksps_x10;
  uint32_t xfer_per_x1000;
  HAL_StatusTypeDef st = HAL_OK;
  char line[256];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while ((remaining > 0U) && (st == HAL_OK))
  {
    uint32_t chunk = remaining;

    if (chunk > SPI_STREAM_CHUNK_MAX)
    {
      chunk = SPI_STREAM_CHUNK_MAX;
    }

    st = Intan_ConvertPipelineDmaTimCsRead(chunk, channel, flags, s_spi_buf);
    total += chunk;
    remaining -= chunk;
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, total);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  xfer_per_x1000 = (total > 0U) ? ((s_stats.spi_xfer32_count * 1000U) / total) : 0U;
  ksps_x10 = usb_stream_rate_ksps_x10(total, elapsed_ms);

  {
    IntanSpiDiagSnapshot clk;
    uint32_t ksps_cyc_x10 = 0U;
    uint32_t wall_ksps_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.sample_period_avg_cycles > 0U)
    {
      ksps_cyc_x10 = Intan_SpiDiag_KspsFromCycX10(clk.sample_period_avg_cycles);
    }
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
    }

    (void)snprintf(line, sizeof(line),
                   "spi_rate samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                   "sck_khz=%lu cyc_samp=%lu ksps_cyc_x10=%lu "
                   "wall_cyc=%lu wall_ksps_x10=%lu st=%lu",
                   (unsigned long)total,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)xfer_per_x1000,
                   (unsigned long)(clk.spi_sck_hz_calc / 1000U),
                   (unsigned long)clk.sample_period_avg_cycles,
                   (unsigned long)ksps_cyc_x10,
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10,
                   (unsigned long)st);
  }
  usb_reply_text(line);
  Intan_DmaPathRelease();
}

static void usb_cmd_spi_rate_fast(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t remaining = n;
  uint32_t total = 0U;
  uint32_t ksps_x10;
  uint32_t xfer_per_x1000;
  HAL_StatusTypeDef st = HAL_OK;
  char line[256];

  if (Intan_SPI_IsReady() == 0U || n == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while ((remaining > 0U) && (st == HAL_OK))
  {
    uint32_t chunk = remaining;

    if (chunk > SPI_STREAM_CHUNK_MAX)
    {
      chunk = SPI_STREAM_CHUNK_MAX;
    }

    st = Intan_ConvertPipelineRead(chunk, channel, flags, s_spi_buf);
    total += chunk;
    remaining -= chunk;
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, total);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  xfer_per_x1000 = (total > 0U) ? ((s_stats.spi_xfer32_count * 1000U) / total) : 0U;
  ksps_x10 = usb_stream_rate_ksps_x10(total, elapsed_ms);

  {
    IntanSpiDiagSnapshot clk;
    uint32_t wall_ksps_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
    }

    (void)snprintf(line, sizeof(line),
                   "spi_rate_fast samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                   "sck_khz=%lu wall_cyc=%lu wall_ksps_x10=%lu st=%lu",
                   (unsigned long)total,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)xfer_per_x1000,
                   (unsigned long)(clk.spi_sck_hz_calc / 1000U),
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10,
                   (unsigned long)st);
  }
  usb_reply_text(line);
}

static void usb_cmd_spi_to_ram(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t ksps_x10;
  char line[256];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_stream_reset_all();
  memset(&s_stats, 0, sizeof(s_stats));
  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  IntanStream_Begin();

  s_spi_remaining = n;
  s_spi_channel = channel;
  s_spi_flags = flags;
  s_spi_counter_mode = 1U;
  s_spi_usb_pump = 0U;
  s_spi_active = 1U;
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while (s_spi_active != 0U)
  {
    if (usb_spi_run_one_chunk() == 0U)
    {
      s_spi_active = 0U;
      IntanStream_End();
      break;
    }
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, s_stats.samples_produced);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  s_stats.responses_pushed = s_stats.samples_produced;
  ksps_x10 = usb_stream_rate_ksps_x10(s_stats.samples_produced, elapsed_ms);

  {
    IntanSpiDiagSnapshot clk;
    uint32_t wall_ksps_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
    }

    (void)snprintf(line, sizeof(line),
                   "spi_to_ram samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "spi_xfer32=%lu xfer_per_resp_x1000=%lu frames_prod=%lu "
                   "wall_cyc=%lu wall_ksps_x10=%lu",
                   (unsigned long)s_stats.samples_produced,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)((s_stats.samples_produced > 0U) ?
                                       ((s_stats.spi_xfer32_count * 1000U) /
                                        s_stats.samples_produced) :
                                       0U),
                   (unsigned long)s_stats.frames_produced,
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10);
  }
  usb_reply_text(line);
  Intan_DmaPathRelease();
}

static void usb_cmd_spi_to_ram_fast(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t remaining = n;
  uint32_t total = 0U;
  uint32_t ksps_x10;
  uint32_t xfer_per_x1000;
  HAL_StatusTypeDef st = HAL_OK;
  char line[256];

  if (Intan_SPI_IsReady() == 0U || n == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while ((remaining > 0U) && (st == HAL_OK))
  {
    uint32_t chunk = remaining;

    if (chunk > SPI_STREAM_CHUNK_MAX)
    {
      chunk = SPI_STREAM_CHUNK_MAX;
    }

    st = Intan_ConvertPipelineRead(chunk, channel, flags, s_spi_buf);
    total += chunk;
    remaining -= chunk;
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, total);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  xfer_per_x1000 = (total > 0U) ? ((s_stats.spi_xfer32_count * 1000U) / total) : 0U;
  ksps_x10 = usb_stream_rate_ksps_x10(total, elapsed_ms);

  {
    IntanSpiDiagSnapshot clk;
    uint32_t wall_ksps_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
    }

    (void)snprintf(line, sizeof(line),
                   "spi_to_ram_fast samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                   "wall_cyc=%lu wall_ksps_x10=%lu st=%lu",
                   (unsigned long)total,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)xfer_per_x1000,
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10,
                   (unsigned long)st);
  }
  usb_reply_text(line);
}

static void usb_cmd_spi_rate_rr8(uint32_t n, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t remaining = n;
  uint32_t total = 0U;
  uint32_t ksps_x10;
  uint32_t ksps_per_ch_x10;
  uint32_t xfer_per_x1000;
  uint8_t phase = 0U;
  HAL_StatusTypeDef st = HAL_OK;
  char line[288];

  if (Intan_SPI_IsReady() == 0U || n == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while ((remaining > 0U) && (st == HAL_OK))
  {
    uint32_t chunk = remaining;

    if (chunk > SPI_STREAM_CHUNK_MAX)
    {
      chunk = SPI_STREAM_CHUNK_MAX;
    }

    st = Intan_ConvertPipelineDmaTimCsReadRR(chunk, INTAN_STREAM_RR8_CHANNELS, flags, s_spi_buf,
                                             &phase);
    total += chunk;
    remaining -= chunk;
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, total);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  xfer_per_x1000 = (total > 0U) ? ((s_stats.spi_xfer32_count * 1000U) / total) : 0U;
  ksps_x10 = usb_stream_rate_ksps_x10(total, elapsed_ms);
  ksps_per_ch_x10 = ksps_x10 / INTAN_STREAM_RR8_CHANNELS;

  {
    IntanSpiDiagSnapshot clk;
    uint32_t wall_ksps_x10 = 0U;
    uint32_t wall_per_ch_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
      wall_per_ch_x10 = wall_ksps_x10 / INTAN_STREAM_RR8_CHANNELS;
    }

    (void)snprintf(line, sizeof(line),
                   "spi_rate_rr8 n_ch=%u samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "ksps_per_ch_x10=%lu spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                   "sck_khz=%lu wall_cyc=%lu wall_ksps_x10=%lu wall_per_ch_x10=%lu "
                   "phase_end=%u st=%lu",
                   (unsigned)INTAN_STREAM_RR8_CHANNELS,
                   (unsigned long)total,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)ksps_per_ch_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)xfer_per_x1000,
                   (unsigned long)(clk.spi_sck_hz_calc / 1000U),
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10,
                   (unsigned long)wall_per_ch_x10,
                   (unsigned)phase,
                   (unsigned long)st);
  }
  usb_reply_text(line);
  Intan_DmaPathRelease();
}

static void usb_cmd_spi_to_ram_rr8(uint32_t n, uint8_t flags)
{
  uint32_t t0_dwt;
  uint32_t t0_ms;
  uint32_t elapsed_ms;
  uint32_t ksps_x10;
  uint32_t ksps_per_ch_x10;
  char line[288];

  if (Intan_SPI_IsReady() == 0U || n == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_stream_reset_all();
  memset(&s_stats, 0, sizeof(s_stats));
  Intan_SpiStats_Reset();
  Intan_SpiDiag_ResetTiming();
  Intan_SpiDiag_Init();
  IntanStream_Begin();

  s_spi_remaining = n;
  s_spi_channel = 0U;
  s_spi_flags = flags;
  s_spi_counter_mode = 1U;
  s_spi_usb_pump = 0U;
  s_spi_rr_first = 0U;
  s_spi_rr_channels = INTAN_STREAM_RR8_CHANNELS;
  s_spi_rr_phase = 0U;
  s_spi_active = 1U;
  t0_ms = HAL_GetTick();
  t0_dwt = DWT->CYCCNT;

  while (s_spi_active != 0U)
  {
    if (usb_spi_run_one_chunk() == 0U)
    {
      s_spi_active = 0U;
      IntanStream_End();
      break;
    }
  }

  Intan_SpiDiag_RecordWall(t0_dwt, DWT->CYCCNT, s_stats.samples_produced);
  elapsed_ms = HAL_GetTick() - t0_ms;
  if (elapsed_ms == 0U)
  {
    elapsed_ms = 1U;
  }

  s_stats.spi_xfer32_count = Intan_SpiStats_GetXfer32Count();
  s_stats.responses_pushed = s_stats.samples_produced;
  ksps_x10 = usb_stream_rate_ksps_x10(s_stats.samples_produced, elapsed_ms);
  ksps_per_ch_x10 = ksps_x10 / INTAN_STREAM_RR8_CHANNELS;

  {
    IntanSpiDiagSnapshot clk;
    uint32_t wall_ksps_x10 = 0U;
    uint32_t wall_per_ch_x10 = 0U;

    Intan_SpiDiag_ReadClockConfig(&clk);
    if (clk.wall_cyc_per_sample > 0U)
    {
      wall_ksps_x10 = Intan_SpiDiag_KspsFromCycX10(clk.wall_cyc_per_sample);
      wall_per_ch_x10 = wall_ksps_x10 / INTAN_STREAM_RR8_CHANNELS;
    }

    (void)snprintf(line, sizeof(line),
                   "spi_to_ram_rr8 n_ch=%u samples=%lu elapsed_ms=%lu ksps_x10=%lu "
                   "ksps_per_ch_x10=%lu spi_xfer32=%lu xfer_per_resp_x1000=%lu "
                   "frames_prod=%lu wall_cyc=%lu wall_ksps_x10=%lu wall_per_ch_x10=%lu "
                   "phase_end=%u",
                   (unsigned)INTAN_STREAM_RR8_CHANNELS,
                   (unsigned long)s_stats.samples_produced,
                   (unsigned long)elapsed_ms,
                   (unsigned long)ksps_x10,
                   (unsigned long)ksps_per_ch_x10,
                   (unsigned long)s_stats.spi_xfer32_count,
                   (unsigned long)((s_stats.samples_produced > 0U) ?
                                       ((s_stats.spi_xfer32_count * 1000U) /
                                        s_stats.samples_produced) :
                                       0U),
                   (unsigned long)s_stats.frames_produced,
                   (unsigned long)clk.wall_cyc_per_sample,
                   (unsigned long)wall_ksps_x10,
                   (unsigned long)wall_per_ch_x10,
                   (unsigned)s_spi_rr_phase);
  }
  usb_reply_text(line);
  Intan_DmaPathRelease();
}

static void usb_intan_stop_stream(void)
{
  usb_stream_reset_all();
}

static void usb_cmd_id(void)
{
  uint16_t val = 0U;
  uint32_t raw32 = 0U;
  char line[96];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_ReadReg_WithRaw(255U, &val, &raw32) != HAL_OK)
  {
    usb_reply_text("ERR spi");
    return;
  }

  (void)snprintf(line, sizeof(line), "OK ID chip=0x%04X raw32=0x%08lX", (unsigned int)val,
                 (unsigned long)raw32);
  usb_reply_text(line);
}

static void usb_cmd_read(uint32_t reg)
{
  uint16_t val = 0U;
  char line[64];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  if (reg > 255U)
  {
    usb_reply_text("ERR reg");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_ReadReg((uint8_t)reg, &val) != HAL_OK)
  {
    usb_reply_text("ERR spi");
    return;
  }

  (void)snprintf(line, sizeof(line), "OK READ reg=%lu value=0x%04X", (unsigned long)reg,
                 (unsigned int)val);
  usb_reply_text(line);
}

static void usb_cmd_write(uint32_t reg, uint32_t value, uint32_t u, uint32_t m)
{
  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  if (reg > 255U || value > 0xFFFFU)
  {
    usb_reply_text("ERR range");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_WriteReg((uint8_t)reg, (uint16_t)value, (uint8_t)u, (uint8_t)m) != HAL_OK)
  {
    usb_reply_text("ERR spi");
    return;
  }

  usb_reply_text("OK WRITE");
}

static void usb_cmd_init_record(uint32_t ksps)
{
  uint16_t target = (uint16_t)ksps;
  char line[48];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  if (target == 0U)
  {
    target = 480U;
  }

  usb_intan_stop_stream();
  if (Intan_App_InitRecord(target) != HAL_OK)
  {
    usb_reply_text("ERR init_record");
    return;
  }

  (void)snprintf(line, sizeof(line), "OK INIT_RECORD %u", (unsigned)target);
  usb_reply_text(line);
}

static void usb_cmd_init_stim(void)
{
  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_App_InitStim() != HAL_OK)
  {
    usb_reply_text("ERR init_stim");
    return;
  }

  usb_reply_text("OK INIT_STIM");
}

static void usb_cmd_clear_adc(void)
{
  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_App_ClearAdc() != HAL_OK)
  {
    usb_reply_text("ERR clear_adc");
    return;
  }

  usb_reply_text("OK CLEAR_ADC");
}

static void usb_cmd_clear_comp(void)
{
  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_App_ClearCompliance() != HAL_OK)
  {
    usb_reply_text("ERR clear_comp");
    return;
  }

  usb_reply_text("OK CLEAR_COMP");
}

static void usb_cmd_convert(uint32_t channel, uint32_t flags)
{
  uint16_t value = 0U;
  char line[80];

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }

  if (channel > 63U)
  {
    usb_reply_text("ERR ch");
    return;
  }

  usb_intan_stop_stream();
  if (Intan_Convert((uint8_t)channel, (uint8_t)flags, &value) != HAL_OK)
  {
    usb_reply_text("ERR spi");
    return;
  }

  (void)snprintf(line, sizeof(line), "OK CONVERT ch=%lu flags=0x%02lX value=0x%04X",
                 (unsigned long)channel, (unsigned long)(flags & 0xFFU), (unsigned int)value);
  usb_reply_text(line);
}

static void usb_cmd_impedance_measure(uint32_t channel, uint32_t scale_bits, uint32_t freq_hz, uint32_t packed)
{
  IntanImpedanceTimedArg arg;
  IntanImpedanceTimedResult res;
  uint32_t samples_per_period = packed & 0xFFFFU;
  uint32_t periods = (packed >> 16) & 0x0FFFU;
  uint32_t flags = (packed >> 28) & 0x0FU;
  uint32_t adc_mean_x1000;
  char line[USB_REPLY_MAX];
  size_t pos = 0U;

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }
  if (s_spi_active != 0U || s_synth_active != 0U)
  {
    usb_reply_text("ERR busy");
    return;
  }
  if (channel > 15U || (scale_bits != 0U && scale_bits != 1U && scale_bits != 3U) ||
      freq_hz < 10U || freq_hz > 10000U ||
      samples_per_period < 4U || samples_per_period > 128U ||
      periods == 0U || periods > 1000U)
  {
    usb_reply_text("ERR range");
    return;
  }

  Intan_DmaPathRelease();
  arg.channel = (uint8_t)channel;
  arg.scale_bits = (uint8_t)scale_bits;
  arg.freq_hz = (uint16_t)freq_hz;
  arg.samples_per_period = (uint16_t)samples_per_period;
  arg.periods = (uint16_t)periods;
  arg.flags = (uint8_t)flags;

  if (Intan_MeasureImpedanceTimed(&arg, &res) != HAL_OK)
  {
    (void)snprintf(line, sizeof(line), "ERR impedance spi_errors=%lu overruns=%lu samples=%lu",
                   (unsigned long)res.spi_errors, (unsigned long)res.overruns,
                   (unsigned long)res.sample_count);
    usb_reply_text(line);
    return;
  }

  adc_mean_x1000 =
      (res.sample_count != 0U) ? (uint32_t)((res.adc_sum * 1000LL) / (int64_t)res.sample_count) : 0U;

  pos = usb_append_str(line, sizeof(line), pos, "OK IMPEDANCE channel=");
  pos = usb_append_u32(line, sizeof(line), pos, channel);
  pos = usb_append_str(line, sizeof(line), pos, " scale=");
  pos = usb_append_u32(line, sizeof(line), pos, scale_bits);
  pos = usb_append_str(line, sizeof(line), pos, " freq_hz=");
  pos = usb_append_u32(line, sizeof(line), pos, freq_hz);
  pos = usb_append_str(line, sizeof(line), pos, " actual_freq_millihz=");
  pos = usb_append_u32(line, sizeof(line), pos, res.actual_freq_millihz);
  pos = usb_append_str(line, sizeof(line), pos, " samples_per_period=");
  pos = usb_append_u32(line, sizeof(line), pos, samples_per_period);
  pos = usb_append_str(line, sizeof(line), pos, " periods=");
  pos = usb_append_u32(line, sizeof(line), pos, periods);
  pos = usb_append_str(line, sizeof(line), pos, " sample_count=");
  pos = usb_append_u32(line, sizeof(line), pos, res.sample_count);
  pos = usb_append_str(line, sizeof(line), pos, " sin_accum=");
  pos = usb_append_i64(line, sizeof(line), pos, res.sin_accum);
  pos = usb_append_str(line, sizeof(line), pos, " cos_accum=");
  pos = usb_append_i64(line, sizeof(line), pos, res.cos_accum);
  pos = usb_append_str(line, sizeof(line), pos, " adc_min=");
  pos = usb_append_u32(line, sizeof(line), pos, res.adc_min);
  pos = usb_append_str(line, sizeof(line), pos, " adc_max=");
  pos = usb_append_u32(line, sizeof(line), pos, res.adc_max);
  pos = usb_append_str(line, sizeof(line), pos, " adc_mean_x1000=");
  pos = usb_append_u32(line, sizeof(line), pos, adc_mean_x1000);
  pos = usb_append_str(line, sizeof(line), pos, " clipped=");
  pos = usb_append_u32(line, sizeof(line), pos, res.clipped);
  pos = usb_append_str(line, sizeof(line), pos, " overruns=");
  pos = usb_append_u32(line, sizeof(line), pos, res.overruns);
  pos = usb_append_str(line, sizeof(line), pos, " spi_errors=");
  pos = usb_append_u32(line, sizeof(line), pos, res.spi_errors);
  pos = usb_append_str(line, sizeof(line), pos, " elapsed_cycles=");
  pos = usb_append_u32(line, sizeof(line), pos, res.elapsed_cycles);
  pos = usb_append_str(line, sizeof(line), pos, " averages=1 p0_sin=");
  pos = usb_append_i64(line, sizeof(line), pos, res.sin_accum);
  pos = usb_append_str(line, sizeof(line), pos, " p0_cos=");
  (void)usb_append_i64(line, sizeof(line), pos, res.cos_accum);
  usb_reply_text(line);
}

static void usb_cmd_pattern_status(void)
{
  IntanPatternStatus ps;
  char line[128];

  Intan_Pattern_GetStatus(&ps);
  (void)snprintf(line, sizeof(line),
                 "OK PATTERN_STATUS loaded=%u running=%u slots=%lu spi=%lu delays=%lu err=%u",
                 (unsigned)ps.loaded, (unsigned)ps.running, (unsigned long)ps.slot_count,
                 (unsigned long)ps.spi_slots, (unsigned long)ps.delay_slots, (unsigned)ps.last_error);
  usb_reply_text(line);
}

static void usb_cmd_pattern_run(uint32_t repeat)
{
  HAL_StatusTypeDef st;

  if (Intan_SPI_IsReady() == 0U)
  {
    usb_reply_text("ERR spi not ready");
    return;
  }
  if (repeat == 0U || repeat > 10000U)
  {
    usb_reply_text("ERR repeat");
    return;
  }
  if (s_spi_active != 0U || s_synth_active != 0U)
  {
    usb_reply_text("ERR busy");
    return;
  }

  st = Intan_Pattern_Run(repeat);
  usb_reply_text((st == HAL_OK) ? "OK PATTERN_RUN" : "ERR pattern_run");
}
#endif

void UsbStreamService_Init(void)
{
  memset(&s_stats, 0, sizeof(s_stats));
  Intan_SpiDiag_Init();
  usb_stream_reset_all();
  USBD_VENDOR_BULK_SetTxCompleteCallback(usb_stream_on_frame_tx_complete);
}

void UsbStreamService_NoteSample(void)
{
  UsbStreamService_NoteSamples(1U);
}

void UsbStreamService_NoteSamples(uint32_t count)
{
  s_stats.samples_produced += count;
  s_stats.responses_pushed = s_stats.samples_produced;
}

void UsbStreamService_NoteFrameProduced(void)
{
  s_stats.frames_produced++;
}

void UsbStreamService_NoteUsbOverflow(void)
{
  s_stats.usb_overflow_count++;
}

void UsbStreamService_NoteSpiOverflow(void)
{
  s_stats.spi_overflow_count++;
}

uint32_t UsbStreamService_GetSpiOverflowCount(void)
{
  return s_stats.spi_overflow_count;
}

uint32_t UsbStreamService_GetUsbOverflowCount(void)
{
  return s_stats.usb_overflow_count;
}

void UsbVendorBulk_ProcessOutCommands(void)
{
  uint16_t rx_len;
  char line[USB_CMD_RX_MAX];
  UsbCommand cmd;

  if (USBD_VENDOR_BULK_PollRx(s_cmd_rx, sizeof(s_cmd_rx), &rx_len) == 0U)
  {
    return;
  }

  if (rx_len >= sizeof(line))
  {
    rx_len = (uint16_t)(sizeof(line) - 1U);
  }
  memcpy(line, s_cmd_rx, rx_len);
  line[rx_len] = '\0';

  cmd = UsbCommands_ParseLine(line);

  switch (cmd.id)
  {
    case USB_CMD_PING:
      usb_reply_text("PONG");
      break;

    case USB_CMD_STOP:
      usb_stream_reset_all();
      usb_reply_text("OK");
      break;

    case USB_CMD_STATS:
      usb_stats_reply();
      break;

    case USB_CMD_SYNTH_STREAM:
      usb_stream_reset_all();
      memset(&s_stats, 0, sizeof(s_stats));
      s_synth_remaining = cmd.arg0;
      s_next_sample = 0U;
      s_next_frame_seq = 0U;
      s_synth_active = (s_synth_remaining > 0U) ? 1U : 0U;
      usb_reply_text("OK");
      break;

    case USB_CMD_SPI_STREAM:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 1U, 1U, 0U, 0U);
      usb_reply_text("OK");
#endif
      break;

    case USB_CMD_SPI_STREAM_REAL:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg1 == 63U)
      {
        usb_reply_text("ERR real stream ch63 unsupported");
      }
      else if (usb_prepare_real_record_stream((uint8_t)cmd.arg1, 1U, (uint8_t)cmd.arg2) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 0U, 1U, 0U, 0U);
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_REAL_FAST:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg1 == 63U)
      {
        usb_reply_text("ERR real stream ch63 unsupported");
      }
      else if (usb_prepare_real_record_stream((uint8_t)cmd.arg1, 1U, (uint8_t)cmd.arg2) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 0U, 1U, 0U, 0U);
        s_spi_safe_polling = SPI_REAL_PATH_FAST_POLLING; /* Diagnostic: register polling, no TIM+DMA CS. */
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_REAL_LEGACY:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg1 == 63U)
      {
        usb_reply_text("ERR real stream ch63 unsupported");
      }
      else if (usb_prepare_real_record_stream((uint8_t)cmd.arg1, 1U, (uint8_t)cmd.arg2) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 0U, 1U, 0U, 0U);
        s_spi_safe_polling = SPI_REAL_PATH_DMA_TIMCS; /* Diagnostic: old free-running TIM+DMA CS path. */
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_REAL_SLOT:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg1 == 63U)
      {
        usb_reply_text("ERR real stream ch63 unsupported");
      }
      else if (usb_prepare_real_record_stream((uint8_t)cmd.arg1, 1U, (uint8_t)cmd.arg2) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 0U, 1U, 0U, 0U);
        s_spi_safe_polling = SPI_REAL_PATH_DMA_TIMSLOT;
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_RR8:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 1U, 1U, 0U, INTAN_STREAM_RR8_CHANNELS);
      usb_reply_text("OK");
#endif
      break;

    case USB_CMD_SPI_STREAM_RR8_REAL:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (usb_prepare_real_record_stream(0U, INTAN_STREAM_RR8_CHANNELS, (uint8_t)cmd.arg1) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 0U, 1U, 0U, INTAN_STREAM_RR8_CHANNELS);
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_RR8_REAL_SLOT:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (usb_prepare_real_record_stream(0U, INTAN_STREAM_RR8_CHANNELS, (uint8_t)cmd.arg1) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 0U, 1U, 0U, INTAN_STREAM_RR8_CHANNELS);
        s_spi_safe_polling = SPI_REAL_PATH_DMA_TIMSLOT;
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_RR16_REAL:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (usb_prepare_real_record_stream(0U, INTAN_STREAM_RR16_CHANNELS, (uint8_t)cmd.arg1) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 0U, 1U, 0U, INTAN_STREAM_RR16_CHANNELS);
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_STREAM_RANGE_REAL:
    case USB_CMD_SPI_STREAM_RANGE_REAL_SLOT:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg1 >= INTAN_STREAM_RR16_CHANNELS || cmd.arg2 == 0U ||
          (cmd.arg1 + cmd.arg2) > INTAN_STREAM_RR16_CHANNELS)
      {
        usb_reply_text("ERR range");
      }
      else if (usb_prepare_real_record_stream((uint8_t)cmd.arg1, (uint8_t)cmd.arg2,
                                              (uint8_t)cmd.arg3) != HAL_OK)
      {
        usb_reply_text("ERR init_record");
      }
      else
      {
        usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg3, 0U, 1U,
                             (uint8_t)cmd.arg1, (uint8_t)cmd.arg2);
        s_spi_safe_polling = SPI_REAL_PATH_DMA_TIMSLOT;
        usb_reply_text("OK");
      }
#endif
      break;

    case USB_CMD_SPI_TO_RAM:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_to_ram(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2);
#endif
      break;

    case USB_CMD_SPI_TO_RAM_FAST:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_to_ram_fast(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2);
#endif
      break;

    case USB_CMD_SPI_RATE:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_rate(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2);
#endif
      break;

    case USB_CMD_SPI_RATE_FAST:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_rate_fast(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2);
#endif
      break;

    case USB_CMD_SPI_RATE_RR8:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_rate_rr8(cmd.arg0, (uint8_t)cmd.arg1);
#endif
      break;

    case USB_CMD_SPI_TO_RAM_RR8:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_spi_to_ram_rr8(cmd.arg0, (uint8_t)cmd.arg1);
#endif
      break;

    case USB_CMD_ID:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_id();
#endif
      break;

    case USB_CMD_READ:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_read(cmd.arg0);
#endif
      break;

    case USB_CMD_WRITE:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_write(cmd.arg0, cmd.arg1, cmd.arg2, cmd.arg3);
#endif
      break;

    case USB_CMD_INIT_RECORD:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_init_record(cmd.arg0);
#endif
      break;

    case USB_CMD_INIT_STIM:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_init_stim();
#endif
      break;

    case USB_CMD_CLEAR_ADC:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_clear_adc();
#endif
      break;

    case USB_CMD_CLEAR_COMP:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_clear_comp();
#endif
      break;

    case USB_CMD_CONVERT:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_convert(cmd.arg0, cmd.arg1);
#endif
      break;

    case USB_CMD_IMPEDANCE_MEASURE:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_impedance_measure(cmd.arg0, cmd.arg1, cmd.arg2, cmd.arg3);
#endif
      break;

    case USB_CMD_PATTERN_CLEAR:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      Intan_Pattern_Clear();
      usb_reply_text("OK PATTERN_CLEAR");
#endif
      break;

    case USB_CMD_PATTERN_ADD_RAW:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_reply_text((Intan_Pattern_AddRawWord(cmd.arg0) == HAL_OK) ? "OK PATTERN_ADD_RAW" : "ERR pattern_add");
#endif
      break;

    case USB_CMD_PATTERN_ADD_WRITE:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg0 > 255U || cmd.arg1 > 0xFFFFU)
      {
        usb_reply_text("ERR range");
      }
      else
      {
        usb_reply_text((Intan_Pattern_AddWrite((uint8_t)cmd.arg0, (uint16_t)cmd.arg1,
                                               (uint8_t)cmd.arg2, (uint8_t)cmd.arg3) == HAL_OK)
                           ? "OK PATTERN_ADD_WRITE"
                           : "ERR pattern_add");
      }
#endif
      break;

    case USB_CMD_PATTERN_ADD_READ:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg0 > 255U)
      {
        usb_reply_text("ERR reg");
      }
      else
      {
        usb_reply_text((Intan_Pattern_AddRead((uint8_t)cmd.arg0) == HAL_OK) ? "OK PATTERN_ADD_READ"
                                                                            : "ERR pattern_add");
      }
#endif
      break;

    case USB_CMD_PATTERN_ADD_CONVERT:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      if (cmd.arg0 > 63U || cmd.arg1 > 255U)
      {
        usb_reply_text("ERR range");
      }
      else
      {
        usb_reply_text((Intan_Pattern_AddConvert((uint8_t)cmd.arg0, (uint8_t)cmd.arg1) == HAL_OK)
                           ? "OK PATTERN_ADD_CONVERT"
                           : "ERR pattern_add");
      }
#endif
      break;

    case USB_CMD_PATTERN_ADD_CLEAR_ADC:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_reply_text((Intan_Pattern_AddClearAdc() == HAL_OK) ? "OK PATTERN_ADD_CLEAR_ADC" : "ERR pattern_add");
#endif
      break;

    case USB_CMD_PATTERN_ADD_CLEAR_COMP:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_reply_text((Intan_Pattern_AddClearCompliance() == HAL_OK) ? "OK PATTERN_ADD_CLEAR_COMP"
                                                                    : "ERR pattern_add");
#endif
      break;

    case USB_CMD_PATTERN_ADD_DELAY_CYC:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_reply_text((Intan_Pattern_AddDelayCycles(cmd.arg0) == HAL_OK) ? "OK PATTERN_ADD_DELAY_CYC"
                                                                        : "ERR pattern_add");
#endif
      break;

    case USB_CMD_PATTERN_ADD_DELAY_US:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_reply_text((Intan_Pattern_AddDelayUs(cmd.arg0) == HAL_OK) ? "OK PATTERN_ADD_DELAY_US"
                                                                    : "ERR pattern_add");
#endif
      break;

    case USB_CMD_PATTERN_STATUS:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_pattern_status();
#endif
      break;

    case USB_CMD_PATTERN_RUN:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_cmd_pattern_run(cmd.arg0);
#endif
      break;

    default:
      usb_reply_text("ERR unknown");
      break;
  }
}

void UsbStreamService_Process(void)
{
  UsbStreamFrame *frame;
  uint32_t chunk;

  usb_spi_stream_process();

  if (s_synth_active == 0U)
  {
    return;
  }

  while (s_synth_remaining > 0U)
  {
    frame = UsbStreamRing_AcquireFilling();
    if (frame == NULL)
    {
      s_stats.usb_overflow_count++;
      break;
    }

    chunk = s_synth_remaining;
    if (chunk > USB_STREAM_FRAME_RESPONSES)
    {
      chunk = USB_STREAM_FRAME_RESPONSES;
    }

    UsbStreamFrame_FillSynth(frame, s_next_frame_seq, s_next_sample, chunk,
                             s_stats.spi_overflow_count, s_stats.usb_overflow_count);
    if (UsbStreamRing_MarkReady(frame) == 0U)
    {
      UsbStreamRing_MarkFree(frame);
      s_stats.usb_overflow_count++;
      break;
    }

    s_next_frame_seq++;
    s_next_sample += chunk;
    s_synth_remaining -= chunk;
    s_stats.samples_produced += chunk;
    s_stats.frames_produced++;
  }

  if (s_synth_remaining == 0U)
  {
    s_synth_active = 0U;
  }
}

void UsbStreamService_TxPump(void)
{
  usb_stream_tx_pump();
}

const UsbStreamStats *UsbStreamService_GetStats(void)
{
  return &s_stats;
}
