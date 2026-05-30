#include "usb_commands.h"
#include <stdlib.h>
#include <string.h>

static int streq_ci(const char *a, const char *b)
{
  if (a == NULL || b == NULL)
  {
    return 0;
  }

  while (*a != '\0' && *b != '\0')
  {
    char ca = (*a >= 'a' && *a <= 'z') ? (char)(*a - 32) : *a;
    char cb = (*b >= 'a' && *b <= 'z') ? (char)(*b - 32) : *b;
    if (ca != cb)
    {
      return 0;
    }
    a++;
    b++;
  }

  return (*a == '\0' && *b == '\0') ? 1 : 0;
}

UsbCommand UsbCommands_ParseLine(const char *line)
{
  UsbCommand cmd = {USB_CMD_NONE, 0U, 0U, 0U, 0U};
  char buf[128];
  char *ctx = NULL;
  char *tok;
  size_t n;

  if (line == NULL)
  {
    return cmd;
  }

  n = strlen(line);
  if (n >= sizeof(buf))
  {
    n = sizeof(buf) - 1U;
  }
  memcpy(buf, line, n);
  buf[n] = '\0';

  while (n > 0U && (buf[n - 1U] == '\n' || buf[n - 1U] == '\r' || buf[n - 1U] == ' '))
  {
    buf[--n] = '\0';
  }

  tok = strtok_r(buf, " \t", &ctx);
  if (tok == NULL)
  {
    return cmd;
  }

  if (streq_ci(tok, "PING"))
  {
    cmd.id = USB_CMD_PING;
    return cmd;
  }

  if (streq_ci(tok, "STOP"))
  {
    cmd.id = USB_CMD_STOP;
    return cmd;
  }

  if (streq_ci(tok, "STATS"))
  {
    cmd.id = USB_CMD_STATS;
    return cmd;
  }

  if (streq_ci(tok, "SYNTH_STREAM"))
  {
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.id = USB_CMD_SYNTH_STREAM;
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "INIT_RECORD"))
  {
    cmd.id = USB_CMD_INIT_RECORD;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "INIT_STIM"))
  {
    cmd.id = USB_CMD_INIT_STIM;
    return cmd;
  }

  if (streq_ci(tok, "CLEAR_ADC"))
  {
    cmd.id = USB_CMD_CLEAR_ADC;
    return cmd;
  }

  if (streq_ci(tok, "CLEAR_COMP"))
  {
    cmd.id = USB_CMD_CLEAR_COMP;
    return cmd;
  }

  if (streq_ci(tok, "CONVERT"))
  {
    cmd.id = USB_CMD_CONVERT;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    if (tok != NULL)
    {
      cmd.arg1 = (uint32_t)strtoul(tok, NULL, 0);
      tok = strtok_r(NULL, " \t", &ctx);
      if (tok != NULL)
      {
        cmd.arg1 |= ((uint32_t)strtoul(tok, NULL, 0) != 0U) ? 2U : 0U;
      }
    }
    return cmd;
  }

  if (streq_ci(tok, "READ"))
  {
    cmd.id = USB_CMD_READ;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "WRITE"))
  {
    cmd.id = USB_CMD_WRITE;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg3 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "ID"))
  {
    cmd.id = USB_CMD_ID;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_CLEAR"))
  {
    cmd.id = USB_CMD_PATTERN_CLEAR;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_RAW"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_RAW;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_WRITE"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_WRITE;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg3 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_READ"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_READ;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_CONVERT"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_CONVERT;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_CLEAR_ADC"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_CLEAR_ADC;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_CLEAR_COMP"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_CLEAR_COMP;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_DELAY_CYC"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_DELAY_CYC;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_ADD_DELAY_US"))
  {
    cmd.id = USB_CMD_PATTERN_ADD_DELAY_US;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_STATUS"))
  {
    cmd.id = USB_CMD_PATTERN_STATUS;
    return cmd;
  }

  if (streq_ci(tok, "PATTERN_RUN"))
  {
    cmd.id = USB_CMD_PATTERN_RUN;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 0) : 1U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RR8_REAL"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RR8_REAL;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RR8_REAL_SLOT"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RR8_REAL_SLOT;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RR16_REAL"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RR16_REAL;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RANGE_REAL"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RANGE_REAL;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg3 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RANGE_REAL_SLOT"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RANGE_REAL_SLOT;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg3 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_RR8"))
  {
    cmd.id = USB_CMD_SPI_STREAM_RR8;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_REAL_FAST"))
  {
    cmd.id = USB_CMD_SPI_STREAM_REAL_FAST;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_REAL_SLOT"))
  {
    cmd.id = USB_CMD_SPI_STREAM_REAL_SLOT;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_REAL_LEGACY"))
  {
    cmd.id = USB_CMD_SPI_STREAM_REAL_LEGACY;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM_REAL"))
  {
    cmd.id = USB_CMD_SPI_STREAM_REAL;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_STREAM"))
  {
    cmd.id = USB_CMD_SPI_STREAM;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_TO_RAM_RR8"))
  {
    cmd.id = USB_CMD_SPI_TO_RAM_RR8;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_TO_RAM_FAST"))
  {
    cmd.id = USB_CMD_SPI_TO_RAM_FAST;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_TO_RAM"))
  {
    cmd.id = USB_CMD_SPI_TO_RAM;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_RATE_RR8"))
  {
    cmd.id = USB_CMD_SPI_RATE_RR8;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_RATE_FAST"))
  {
    cmd.id = USB_CMD_SPI_RATE_FAST;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  if (streq_ci(tok, "SPI_RATE"))
  {
    cmd.id = USB_CMD_SPI_RATE;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg0 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg1 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    tok = strtok_r(NULL, " \t", &ctx);
    cmd.arg2 = (tok != NULL) ? (uint32_t)strtoul(tok, NULL, 10) : 0U;
    return cmd;
  }

  cmd.id = USB_CMD_UNKNOWN;
  return cmd;
}
