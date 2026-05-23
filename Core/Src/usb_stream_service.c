#include "usb_stream_service.h"
#include "usb_commands.h"
#include "usb_stream_ring.h"
#include "usb_stream_frame.h"
#include "usb_vendor_bulk.h"
#include "intan_stream.h"
#include "intan_spi.h"
#include "intan_spi_diag.h"
#include <stdio.h>
#include <string.h>

#define USB_CMD_RX_MAX        256U
#define USB_REPLY_MAX         256U
#define SPI_STREAM_CHUNK_MAX  (INTAN_DMA_CHUNK_SLOTS - 2U)
#define SPI_CHUNKS_PER_TICK   8U

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
static uint8_t s_spi_rr8;
static uint8_t s_spi_rr_phase;

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
static void usb_stats_reply(void);

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

static void usb_stream_reset_all(void)
{
  s_synth_active = 0U;
  s_synth_remaining = 0U;
  s_spi_active = 0U;
  s_spi_remaining = 0U;
  s_spi_rr8 = 0U;
  s_spi_rr_phase = 0U;
  s_tx_active = 0U;
  s_tx_frame = NULL;
  IntanStream_Reset();
  UsbStreamRing_Reset();
  Intan_DmaPathRelease();
}

static void usb_spi_stream_start(uint32_t n, uint8_t channel, uint8_t flags, uint8_t counter_mode,
                                 uint8_t usb_pump, uint8_t rr8)
{
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
  s_spi_rr8 = rr8;
  s_spi_rr_phase = 0U;
  s_spi_active = (n > 0U) ? 1U : 0U;

  IntanStream_Begin();
}

static uint8_t usb_spi_run_one_chunk(void)
{
  uint32_t chunk;
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

  if (s_spi_rr8 != 0U)
  {
    st = Intan_ConvertPipelineDmaTimCsReadRR(chunk, INTAN_STREAM_RR8_CHANNELS, s_spi_flags,
                                             s_spi_buf, &s_spi_rr_phase);
  }
  else
  {
    st = Intan_ConvertPipelineDmaTimCsRead(chunk, s_spi_channel, s_spi_flags, s_spi_buf);
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
  ksps_x10 = (total * 10000U) / elapsed_ms;

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
  ksps_x10 = (total * 10000U) / elapsed_ms;

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
  ksps_x10 = (s_stats.samples_produced * 10000U) / elapsed_ms;

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
  ksps_x10 = (total * 10000U) / elapsed_ms;

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
  ksps_x10 = (total * 10000U) / elapsed_ms;
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
  s_spi_rr8 = 1U;
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
  ksps_x10 = (s_stats.samples_produced * 10000U) / elapsed_ms;
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
      usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 1U, 1U, 0U);
      usb_reply_text("OK");
#endif
      break;

    case USB_CMD_SPI_STREAM_REAL:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_spi_stream_start(cmd.arg0, (uint8_t)cmd.arg1, (uint8_t)cmd.arg2, 0U, 1U, 0U);
      usb_reply_text("OK");
#endif
      break;

    case USB_CMD_SPI_STREAM_RR8:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 1U, 1U, 1U);
      usb_reply_text("OK");
#endif
      break;

    case USB_CMD_SPI_STREAM_RR8_REAL:
#if (INTAN_HW_PRESENT == 0)
      usb_reply_text("ERR no intan hw");
#else
      usb_spi_stream_start(cmd.arg0, 0U, (uint8_t)cmd.arg1, 0U, 1U, 1U);
      usb_reply_text("OK");
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
