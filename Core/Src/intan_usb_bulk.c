#include "intan_usb_bulk.h"
#include "intan_app.h"
#include "intan_spi.h"
#include "usbd_vendor_bulk.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define USB_CMD_MAX   VENDOR_BULK_HS_MAX_PACKET
#define USB_REPLY_MAX VENDOR_BULK_HS_MAX_PACKET
#define USB_STREAM_BULK_PACKET_SAMPLES (VENDOR_BULK_HS_MAX_PACKET / 2U)
#define USB_STREAM_DMA_SAMPLES         4096U
#define USB_STREAM_TX_TIMEOUT_MS       5000U
#define USB_STREAM_PINGPONG_BUFS       2U
#define USB_STREAM_RR8_CHANNELS        INTAN_STREAM_RR8_CHANNELS

static uint8_t s_rx[USB_CMD_MAX];
static char s_reply[USB_REPLY_MAX];
static uint16_t s_stream_buf[USB_STREAM_PINGPONG_BUFS][USB_STREAM_DMA_SAMPLES]
    __attribute__((section(".dma_buffer"), aligned(32)));
static uint16_t s_reply_len;

typedef struct
{
  uint16_t *buf;
  uint32_t total_samples;
  uint32_t byte_off;
  uint8_t active;
} UsbStreamOutState;

typedef struct
{
  uint16_t *buf;
  uint32_t samples;
  uint8_t valid;
} UsbStreamPendingOut;

static UsbStreamOutState s_stream_out;
static UsbStreamPendingOut s_stream_pending;

static void stream_usb_pump(void);

static char *skip_ws(char *p)
{
  while (*p == ' ' || *p == '\t')
  {
    p++;
  }
  return p;
}

static int next_token(char **ctx, char *out, size_t outsz)
{
  char *p = skip_ws(*ctx);
  size_t n = 0U;

  if (*p == '\0')
  {
    out[0] = '\0';
    *ctx = p;
    return 0;
  }

  while (*p != '\0' && *p != ' ' && *p != '\t' && n + 1U < outsz)
  {
    out[n++] = *p++;
  }
  out[n] = '\0';
  *ctx = p;
  return (int)n;
}

static void set_reply(const char *text)
{
  int n = snprintf(s_reply, sizeof(s_reply), "%s\n", text);

  if (n < 0)
  {
    s_reply_len = 0U;
    return;
  }
  if ((size_t)n >= sizeof(s_reply))
  {
    n = (int)sizeof(s_reply) - 1;
    s_reply[n] = '\0';
  }
  s_reply_len = (uint16_t)n;
}

static HAL_StatusTypeDef wait_usb_tx_idle(void)
{
  uint32_t t0 = HAL_GetTick();

  while (USBD_VENDOR_BULK_TxIdle() == 0U)
  {
    stream_usb_pump();
    if ((HAL_GetTick() - t0) > USB_STREAM_TX_TIMEOUT_MS)
    {
      return HAL_TIMEOUT;
    }
  }

  return HAL_OK;
}

static void stream_usb_reset_state(void)
{
  s_stream_out.buf = NULL;
  s_stream_out.total_samples = 0U;
  s_stream_out.byte_off = 0U;
  s_stream_out.active = 0U;
  s_stream_pending.buf = NULL;
  s_stream_pending.samples = 0U;
  s_stream_pending.valid = 0U;
}

static void stream_usb_start_send(uint16_t *buf, uint32_t samples)
{
  s_stream_out.buf = buf;
  s_stream_out.total_samples = samples;
  s_stream_out.byte_off = 0U;
  s_stream_out.active = 1U;
}

static void stream_usb_queue_send(uint16_t *buf, uint32_t samples)
{
  if (s_stream_out.active == 0U)
  {
    stream_usb_start_send(buf, samples);
    return;
  }

  s_stream_pending.buf = buf;
  s_stream_pending.samples = samples;
  s_stream_pending.valid = 1U;
}

static void stream_usb_pump(void)
{
  uint32_t total_bytes;
  uint32_t remaining;
  uint16_t packet_bytes;

  if ((s_stream_out.active == 0U) && (s_stream_pending.valid != 0U))
  {
    stream_usb_start_send(s_stream_pending.buf, s_stream_pending.samples);
    s_stream_pending.valid = 0U;
  }

  while (s_stream_out.active != 0U)
  {
    if (USBD_VENDOR_BULK_TxReady() == 0U)
    {
      break;
    }

    total_bytes = s_stream_out.total_samples * 2U;
    if (s_stream_out.byte_off >= total_bytes)
    {
      s_stream_out.active = 0U;
      if (s_stream_pending.valid != 0U)
      {
        stream_usb_start_send(s_stream_pending.buf, s_stream_pending.samples);
        s_stream_pending.valid = 0U;
      }
      break;
    }

    remaining = total_bytes - s_stream_out.byte_off;
    packet_bytes = (remaining > VENDOR_BULK_HS_MAX_PACKET) ? VENDOR_BULK_HS_MAX_PACKET : (uint16_t)remaining;
    if (USBD_VENDOR_BULK_TransmitZc((const uint8_t *)s_stream_out.buf + s_stream_out.byte_off, packet_bytes) != USBD_OK)
    {
      break;
    }

    s_stream_out.byte_off += packet_bytes;
    if (s_stream_out.byte_off >= total_bytes)
    {
      s_stream_out.active = 0U;
      if (s_stream_pending.valid != 0U)
      {
        stream_usb_start_send(s_stream_pending.buf, s_stream_pending.samples);
        s_stream_pending.valid = 0U;
      }
    }
  }
}

static void stream_usb_idle_hook(void *ctx)
{
  (void)ctx;
  stream_usb_pump();
}

static HAL_StatusTypeDef stream_drain_usb(void)
{
  uint32_t t0 = HAL_GetTick();

  while ((s_stream_out.active != 0U) || (s_stream_pending.valid != 0U))
  {
    stream_usb_pump();
    if ((s_stream_out.active == 0U) && (s_stream_pending.valid == 0U))
    {
      break;
    }
    if ((HAL_GetTick() - t0) > USB_STREAM_TX_TIMEOUT_MS)
    {
      return HAL_TIMEOUT;
    }
  }

  return wait_usb_tx_idle();
}

static HAL_StatusTypeDef stream_convert_samples(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t acquired = 0U;
  uint8_t fill_idx = 0U;
  uint32_t block_samples;
  HAL_StatusTypeDef st;

  stream_usb_reset_state();
  Intan_SetIdleHook(stream_usb_idle_hook, NULL);

  block_samples = n;
  if (block_samples > USB_STREAM_DMA_SAMPLES)
  {
    block_samples = USB_STREAM_DMA_SAMPLES;
  }

  st = Intan_ConvertPipelineDmaTimCsRead(block_samples, channel, flags, s_stream_buf[0]);
  if (st != HAL_OK)
  {
    Intan_SetIdleHook(NULL, NULL);
    return st;
  }

  acquired += block_samples;
  stream_usb_queue_send(s_stream_buf[0], block_samples);
  fill_idx = 1U;

  while ((acquired < n) || (s_stream_out.active != 0U) || (s_stream_pending.valid != 0U))
  {
    stream_usb_pump();

    if (acquired < n)
    {
      block_samples = n - acquired;
      if (block_samples > USB_STREAM_DMA_SAMPLES)
      {
        block_samples = USB_STREAM_DMA_SAMPLES;
      }

      st = Intan_ConvertPipelineDmaTimCsRead(block_samples, channel, flags, s_stream_buf[fill_idx]);
      if (st != HAL_OK)
      {
        Intan_SetIdleHook(NULL, NULL);
        return st;
      }

      acquired += block_samples;
      stream_usb_queue_send(s_stream_buf[fill_idx], block_samples);
      fill_idx ^= 1U;
    }
  }

  Intan_SetIdleHook(NULL, NULL);
  return stream_drain_usb();
}

static HAL_StatusTypeDef stream_convert_samples_rr8(uint32_t n, uint8_t flags)
{
  uint32_t acquired = 0U;
  uint8_t fill_idx = 0U;
  uint8_t rr_phase = 0U;
  uint32_t block_samples;
  HAL_StatusTypeDef st;

  stream_usb_reset_state();
  Intan_SetIdleHook(stream_usb_idle_hook, NULL);

  block_samples = n;
  if (block_samples > USB_STREAM_DMA_SAMPLES)
  {
    block_samples = USB_STREAM_DMA_SAMPLES;
  }

  st = Intan_ConvertPipelineDmaTimCsReadRR(block_samples, USB_STREAM_RR8_CHANNELS, flags,
                                           s_stream_buf[0], &rr_phase);
  if (st != HAL_OK)
  {
    Intan_SetIdleHook(NULL, NULL);
    return st;
  }

  acquired += block_samples;
  stream_usb_queue_send(s_stream_buf[0], block_samples);
  fill_idx = 1U;

  while ((acquired < n) || (s_stream_out.active != 0U) || (s_stream_pending.valid != 0U))
  {
    stream_usb_pump();

    if (acquired < n)
    {
      block_samples = n - acquired;
      if (block_samples > USB_STREAM_DMA_SAMPLES)
      {
        block_samples = USB_STREAM_DMA_SAMPLES;
      }

      st = Intan_ConvertPipelineDmaTimCsReadRR(block_samples, USB_STREAM_RR8_CHANNELS, flags,
                                               s_stream_buf[fill_idx], &rr_phase);
      if (st != HAL_OK)
      {
        Intan_SetIdleHook(NULL, NULL);
        return st;
      }

      acquired += block_samples;
      stream_usb_queue_send(s_stream_buf[fill_idx], block_samples);
      fill_idx ^= 1U;
    }
  }

  Intan_SetIdleHook(NULL, NULL);
  return stream_drain_usb();
}

static int parse_bench_n_ch(char **ctx, unsigned long *out_n, uint8_t *out_ch)
{
  char a[24];
  char b[24];

  if (next_token(ctx, a, sizeof(a)) == 0)
  {
    return 0;
  }

  *out_n = strtoul(a, NULL, 0);
  if (*out_n == 0UL || *out_n > 2000000UL)
  {
    return -1;
  }

  *out_ch = 63U;
  if (next_token(ctx, b, sizeof(b)) > 0)
  {
    *out_ch = (uint8_t)strtoul(b, NULL, 0);
  }

  return 1;
}

static void reply_bench_stats(const char *tag, unsigned long n, uint8_t ch, float kt, float kp)
{
  uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
  uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);

  (void)snprintf(s_reply, sizeof(s_reply),
                 "OK %s n=%lu ch=%u ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu\n",
                 tag, n, (unsigned)ch,
                 (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                 (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
  s_reply_len = (uint16_t)strlen(s_reply);
}

static void dispatch_usb_command(char *line)
{
  char *ctx = line;
  char cmd[24];
  char a[24];
  char b[24];
  char c[24];
  unsigned long u0;
  unsigned long u1;
  uint16_t val = 0U;
  uint32_t raw32 = 0U;
  HAL_StatusTypeDef st;

  if (next_token(&ctx, cmd, sizeof(cmd)) == 0)
  {
    set_reply("ERR empty");
    return;
  }

  if (strcmp(cmd, "HELP") == 0 || strcmp(cmd, "?") == 0)
  {
    set_reply("OK CMDS: HELP PING ECHO ID READ r READRAW r WRITE r hex [u m] "
              "INIT_RECORD [ksps] INIT_STIM CLEAR_ADC CLEAR_COMP "
              "CONVERT ch [flags] "
              "BENCH n [ch] BENCH_FAST n [ch] BENCH_DMA n [ch] BENCH_TIMCS n [ch] [target_ksps] "
              "STREAM n [ch] [flags] STREAM8 n [flags]");
    return;
  }

  if (strcmp(cmd, "PING") == 0)
  {
    set_reply("OK PONG");
    return;
  }

  if (strcmp(cmd, "ECHO") == 0)
  {
    char *payload = skip_ws(ctx);
    (void)snprintf(s_reply, sizeof(s_reply), "OK ECHO %s\n", payload);
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

#if (INTAN_HW_PRESENT == 0)
  set_reply("ERR no intan hw");
  return;
#endif

  if (strcmp(cmd, "ID") == 0)
  {
    st = Intan_ReadReg_WithRaw(INTAN_CHIP_ID_REG, &val, &raw32);
    if (st != HAL_OK)
    {
      set_reply("ERR spi");
      return;
    }
    (void)snprintf(s_reply, sizeof(s_reply), "OK ID chip=0x%04X raw32=0x%08lX\n",
                   (unsigned)val, (unsigned long)raw32);
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

  if (strcmp(cmd, "READ") == 0 || strcmp(cmd, "READRAW") == 0)
  {
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      set_reply("ERR args");
      return;
    }

    u0 = strtoul(a, NULL, 0);
    if (u0 > 255U)
    {
      set_reply("ERR reg");
      return;
    }

    if (strcmp(cmd, "READRAW") == 0)
    {
      st = Intan_ReadReg_WithRaw((uint8_t)u0, &val, &raw32);
      if (st != HAL_OK)
      {
        set_reply("ERR spi");
        return;
      }
      (void)snprintf(s_reply, sizeof(s_reply), "OK READRAW reg=%lu value=0x%04X raw32=0x%08lX\n",
                     u0, (unsigned)val, (unsigned long)raw32);
    }
    else
    {
      st = Intan_ReadReg((uint8_t)u0, &val);
      if (st != HAL_OK)
      {
        set_reply("ERR spi");
        return;
      }
      (void)snprintf(s_reply, sizeof(s_reply), "OK READ reg=%lu value=0x%04X\n",
                     u0, (unsigned)val);
    }
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

  if (strcmp(cmd, "WRITE") == 0)
  {
    uint8_t ru = 0U;
    uint8_t rm = 0U;

    if (next_token(&ctx, a, sizeof(a)) == 0 || next_token(&ctx, b, sizeof(b)) == 0)
    {
      set_reply("ERR args");
      return;
    }

    u0 = strtoul(a, NULL, 0);
    u1 = strtoul(b, NULL, 0);
    if (next_token(&ctx, c, sizeof(c)) > 0)
    {
      ru = (uint8_t)strtoul(c, NULL, 0);
    }
    if (next_token(&ctx, c, sizeof(c)) > 0)
    {
      rm = (uint8_t)strtoul(c, NULL, 0);
    }
    if (u0 > 255U || u1 > 0xFFFFU)
    {
      set_reply("ERR range");
      return;
    }

    st = Intan_WriteReg((uint8_t)u0, (uint16_t)u1, ru, rm);
    if (st != HAL_OK)
    {
      set_reply("ERR spi");
      return;
    }
    set_reply("OK WRITE");
    return;
  }

  if (strcmp(cmd, "INIT_STIM") == 0)
  {
    st = Intan_App_InitStim();
    if (st != HAL_OK)
    {
      set_reply("ERR init_stim");
      return;
    }
    set_reply("OK INIT_STIM");
    return;
  }

  if (strcmp(cmd, "INIT_RECORD") == 0)
  {
    uint16_t ksps = 480U;

    if (next_token(&ctx, a, sizeof(a)) > 0)
    {
      ksps = (uint16_t)strtoul(a, NULL, 0);
      if (ksps == 0U)
      {
        ksps = 480U;
      }
    }

    st = Intan_App_InitRecord(ksps);
    if (st != HAL_OK)
    {
      set_reply("ERR init_record");
      return;
    }
    (void)snprintf(s_reply, sizeof(s_reply), "OK INIT_RECORD %u\n", (unsigned)ksps);
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

  if (strcmp(cmd, "CLEAR_ADC") == 0)
  {
    st = Intan_App_ClearAdc();
    if (st != HAL_OK)
    {
      set_reply("ERR clear_adc");
      return;
    }
    set_reply("OK CLEAR_ADC");
    return;
  }

  if (strcmp(cmd, "CLEAR_COMP") == 0)
  {
    st = Intan_App_ClearCompliance();
    if (st != HAL_OK)
    {
      set_reply("ERR clear_comp");
      return;
    }
    set_reply("OK CLEAR_COMP");
    return;
  }

  if (strcmp(cmd, "CONVERT") == 0)
  {
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      set_reply("ERR args");
      return;
    }

    u0 = strtoul(a, NULL, 0);
    u1 = 0U;
    if (next_token(&ctx, b, sizeof(b)) != 0)
    {
      u1 = strtoul(b, NULL, 0);
    }
    if (u0 > 63U || u1 > 255U)
    {
      set_reply("ERR range");
      return;
    }

    st = Intan_Convert((uint8_t)u0, (uint8_t)u1, &val);
    if (st != HAL_OK)
    {
      set_reply("ERR spi");
      return;
    }
    (void)snprintf(s_reply, sizeof(s_reply), "OK CONVERT ch=%lu flags=0x%02lX value=0x%04X\n",
                   u0, u1, (unsigned)val);
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

  if (strcmp(cmd, "BENCH") == 0 || strcmp(cmd, "BENCH_FAST") == 0 ||
      strcmp(cmd, "BENCH_DMA") == 0 || strcmp(cmd, "BENCH_TIMCS") == 0 ||
      strcmp(cmd, "BENCH_TIM") == 0)
  {
    float kt;
    float kp;
    uint8_t ch = 63U;
    uint32_t target_ksps = 600U;
    int parsed;

    parsed = parse_bench_n_ch(&ctx, &u0, &ch);
    if (parsed == 0)
    {
      set_reply("ERR args");
      return;
    }
    if (parsed < 0)
    {
      set_reply("ERR n");
      return;
    }

    if (strcmp(cmd, "BENCH_TIMCS") == 0 || strcmp(cmd, "BENCH_TIM") == 0)
    {
      if (next_token(&ctx, c, sizeof(c)) > 0)
      {
        target_ksps = (uint32_t)strtoul(c, NULL, 0);
        if (target_ksps < 100U || target_ksps > 720U)
        {
          set_reply("ERR target");
          return;
        }
      }
      st = Intan_App_BenchConvertTimCs((uint32_t)u0, ch, target_ksps, &kt, &kp);
      if (st != HAL_OK)
      {
        set_reply("ERR bench_timcs");
        return;
      }
      {
        uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
        uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);
        (void)snprintf(s_reply, sizeof(s_reply),
                       "OK BENCH_TIMCS n=%lu ch=%u target=%lu ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu\n",
                       u0, (unsigned)ch, (unsigned long)target_ksps,
                       (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                       (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
        s_reply_len = (uint16_t)strlen(s_reply);
      }
      return;
    }

    if (strcmp(cmd, "BENCH") == 0)
    {
      st = Intan_App_BenchConvert((uint32_t)u0, ch, &kt, &kp);
      if (st != HAL_OK)
      {
        set_reply("ERR bench");
        return;
      }
      reply_bench_stats("BENCH", u0, ch, kt, kp);
      return;
    }

    if (strcmp(cmd, "BENCH_FAST") == 0)
    {
      st = Intan_App_BenchConvertFast((uint32_t)u0, ch, &kt, &kp);
      if (st != HAL_OK)
      {
        set_reply("ERR bench_fast");
        return;
      }
      reply_bench_stats("BENCH_FAST", u0, ch, kt, kp);
      return;
    }

    st = Intan_App_BenchConvertDmaTimCs((uint32_t)u0, ch, &kt, &kp);
    if (st != HAL_OK)
    {
      set_reply("ERR bench_dma");
      return;
    }
    reply_bench_stats("BENCH_DMA", u0, ch, kt, kp);
    return;
  }

  if (strcmp(cmd, "STREAM") == 0)
  {
    uint8_t channel = 0U;
    uint8_t flags = 0U;

    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      set_reply("ERR args");
      return;
    }

    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL)
    {
      set_reply("ERR n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) != 0)
    {
      u1 = strtoul(b, NULL, 0);
      if (u1 > 63U)
      {
        set_reply("ERR ch");
        return;
      }
      channel = (uint8_t)u1;
    }
    if (next_token(&ctx, b, sizeof(b)) != 0)
    {
      u1 = strtoul(b, NULL, 0);
      if (u1 > 255U)
      {
        set_reply("ERR flags");
        return;
      }
      flags = (uint8_t)u1;
    }

    if (stream_convert_samples((uint32_t)u0, channel, flags) != HAL_OK)
    {
      set_reply("ERR stream");
    }
    return;
  }

  if (strcmp(cmd, "STREAM8") == 0)
  {
    uint8_t flags = 0U;

    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      set_reply("ERR args");
      return;
    }

    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL)
    {
      set_reply("ERR n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) != 0)
    {
      u1 = strtoul(b, NULL, 0);
      if (u1 > 255U)
      {
        set_reply("ERR flags");
        return;
      }
      flags = (uint8_t)u1;
    }

    if (stream_convert_samples_rr8((uint32_t)u0, flags) != HAL_OK)
    {
      set_reply("ERR stream8");
    }
    return;
  }

  set_reply("ERR unknown");
}

void Intan_USB_Bulk_Process(void)
{
  uint16_t len = 0U;

  if (s_reply_len > 0U)
  {
    if (USBD_VENDOR_BULK_Transmit((uint8_t *)s_reply, s_reply_len) == USBD_OK)
    {
      s_reply_len = 0U;
    }
    return;
  }

  if (USBD_VENDOR_BULK_PollRx(s_rx, sizeof(s_rx) - 1U, &len) == 0U)
  {
    return;
  }

  s_rx[len] = '\0';
  for (uint16_t i = 0U; i < len; i++)
  {
    if (s_rx[i] == '\r' || s_rx[i] == '\n')
    {
      s_rx[i] = '\0';
      break;
    }
  }

  dispatch_usb_command((char *)s_rx);
}
