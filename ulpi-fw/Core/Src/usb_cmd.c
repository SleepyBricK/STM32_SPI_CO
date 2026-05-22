#include "usb_cmd.h"
#include "usbd_vendor_bulk.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CMD_MAX    VENDOR_BULK_HS_MAX_PACKET
#define REPLY_MAX  VENDOR_BULK_HS_MAX_PACKET
#define STREAM_MAX 65535U
#define STREAM_PKT (VENDOR_BULK_HS_MAX_PACKET / 2U)
#define TX_TIMEOUT 1000U

static uint8_t s_rx[CMD_MAX];
static char s_reply[REPLY_MAX];
static uint16_t s_stream[STREAM_PKT];
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

static HAL_StatusTypeDef wait_tx(void)
{
  uint32_t t0 = HAL_GetTick();
  while (USBD_VENDOR_BULK_TxReady() == 0U)
  {
    if ((HAL_GetTick() - t0) > TX_TIMEOUT)
    {
      return HAL_TIMEOUT;
    }
  }
  return HAL_OK;
}

static HAL_StatusTypeDef stream_ramp(uint32_t n)
{
  uint32_t sent = 0U;

  while (sent < n)
  {
    uint32_t block = n - sent;
    if (block > STREAM_PKT)
    {
      block = STREAM_PKT;
    }

    for (uint32_t i = 0U; i < block; i++)
    {
      s_stream[i] = (uint16_t)((sent + i) & 0xFFFFU);
    }

    if (wait_tx() != HAL_OK)
    {
      return HAL_TIMEOUT;
    }
    if (USBD_VENDOR_BULK_Transmit((uint8_t *)s_stream, (uint16_t)(block * 2U)) != USBD_OK)
    {
      return HAL_ERROR;
    }
    sent += block;
  }

  return wait_tx();
}

static void dispatch(char *line)
{
  char *ctx = line;
  char cmd[24];
  char arg[24];
  unsigned long n;

  if (next_token(&ctx, cmd, sizeof(cmd)) == 0)
  {
    set_reply("ERR empty");
    return;
  }

  if (strcmp(cmd, "HELP") == 0 || strcmp(cmd, "?") == 0)
  {
    set_reply("OK CMDS: HELP PING ECHO text STATUS STREAM n");
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

  if (strcmp(cmd, "STATUS") == 0)
  {
    const char *rdy = (__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY) != 0U) ? "1" : "0";
    (void)snprintf(s_reply, sizeof(s_reply), "OK FW=WeActULPI USB33RDY=%s TX=%u\n",
                   rdy, (unsigned)USBD_VENDOR_BULK_TxReady());
    s_reply_len = (uint16_t)strlen(s_reply);
    return;
  }

  if (strcmp(cmd, "STREAM") == 0)
  {
    if (next_token(&ctx, arg, sizeof(arg)) == 0)
    {
      set_reply("ERR args");
      return;
    }
    n = strtoul(arg, NULL, 0);
    if (n == 0UL || n > STREAM_MAX)
    {
      set_reply("ERR n");
      return;
    }
    if (stream_ramp((uint32_t)n) != HAL_OK)
    {
      set_reply("ERR stream");
    }
    return;
  }

  set_reply("ERR unknown");
}

void USB_Cmd_Process(void)
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

  dispatch((char *)s_rx);
}
