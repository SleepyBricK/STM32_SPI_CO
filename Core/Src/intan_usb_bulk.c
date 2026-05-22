#include "intan_usb_bulk.h"
#include "intan_spi.h"
#include "usbd_vendor_bulk.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define USB_CMD_MAX   VENDOR_BULK_HS_MAX_PACKET
#define USB_REPLY_MAX VENDOR_BULK_HS_MAX_PACKET
#define USB_STREAM_BULK_PACKET_SAMPLES (VENDOR_BULK_HS_MAX_PACKET / 2U)
#define USB_STREAM_DMA_SAMPLES         4096U
#define USB_STREAM_TX_TIMEOUT_MS       1000U

static uint8_t s_rx[USB_CMD_MAX];
static char s_reply[USB_REPLY_MAX];
static uint16_t s_stream_samples[USB_STREAM_DMA_SAMPLES];
static uint16_t s_reply_len;

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

static HAL_StatusTypeDef wait_usb_tx_ready(void)
{
  uint32_t t0 = HAL_GetTick();

  while (USBD_VENDOR_BULK_TxReady() == 0U)
  {
    if ((HAL_GetTick() - t0) > USB_STREAM_TX_TIMEOUT_MS)
    {
      return HAL_TIMEOUT;
    }
  }

  return HAL_OK;
}

static HAL_StatusTypeDef stream_convert_samples(uint32_t n, uint8_t channel, uint8_t flags)
{
  uint32_t acquired = 0U;

  while (acquired < n)
  {
    uint32_t block_samples = n - acquired;
    uint32_t block_sent = 0U;

    if (block_samples > USB_STREAM_DMA_SAMPLES)
    {
      block_samples = USB_STREAM_DMA_SAMPLES;
    }

    if (Intan_ConvertPipelineDmaTimCsRead(block_samples, channel, flags, s_stream_samples) != HAL_OK)
    {
      return HAL_ERROR;
    }

    while (block_sent < block_samples)
    {
      uint32_t packet_samples = block_samples - block_sent;
      uint16_t packet_bytes;

      if (packet_samples > USB_STREAM_BULK_PACKET_SAMPLES)
      {
        packet_samples = USB_STREAM_BULK_PACKET_SAMPLES;
      }

      packet_bytes = (uint16_t)(packet_samples * 2U);
      if (wait_usb_tx_ready() != HAL_OK)
      {
        return HAL_TIMEOUT;
      }
      if (USBD_VENDOR_BULK_Transmit((uint8_t *)&s_stream_samples[block_sent], packet_bytes) != USBD_OK)
      {
        return HAL_ERROR;
      }

      block_sent += packet_samples;
    }

    acquired += block_samples;
  }

  return wait_usb_tx_ready();
}

static void dispatch_usb_command(char *line)
{
  char *ctx = line;
  char cmd[24];
  char a[24];
  char b[24];
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
    set_reply("OK CMDS: HELP PING ECHO text ID READ r READRAW r CONVERT ch [flags] STREAM n [ch] [flags]");
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
