/**
 * @file intan_uart_cli.c
 * @brief Команды по USART1 RX (115200 8N1). Ответы на TX отключены (тишина на линии).
 */

#include "intan_uart_cli.h"
#include "intan_app.h"
#include "intan_spi.h"
#include "usart.h"
#include "stm32h7xx_hal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define UART_LINE_MAX 160
#define UART_TX_MAX   256
#define UART_IO_TIMEOUT_MS 200U

static uint8_t s_rx_byte;
static char s_line[UART_LINE_MAX];
static volatile uint16_t s_line_len;
static volatile uint8_t s_line_ready;

static void uart_tx_str(const char *s)
{
  (void)s;
  /* Текстовый ответ по UART отключён. Приём команд (RX IT) сохранён. */
}

static void reply_ok(const char *msg)
{
  char buf[UART_TX_MAX];
  if (msg != NULL && msg[0] != '\0')
  {
    (void)snprintf(buf, sizeof(buf), "OK %s\r\n", msg);
  }
  else
  {
    (void)snprintf(buf, sizeof(buf), "OK\r\n");
  }
  uart_tx_str(buf);
}

static void reply_err(const char *msg)
{
  char buf[UART_TX_MAX];
  (void)snprintf(buf, sizeof(buf), "ERR %s\r\n", msg != NULL ? msg : "unknown");
  uart_tx_str(buf);
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  uint8_t c;

  if (huart->Instance != USART1)
  {
    return;
  }

  c = s_rx_byte;

  if (s_line_ready)
  {
    (void)HAL_UART_Receive_IT(&huart1, &s_rx_byte, 1U);
    return;
  }

  if (c == (uint8_t)'\n' || c == (uint8_t)'\r')
  {
    if (s_line_len >= UART_LINE_MAX)
    {
      s_line_len = UART_LINE_MAX - 1U;
    }
    s_line[s_line_len] = '\0';
    s_line_ready = 1U;
    s_line_len = 0U;
  }
  else
  {
    if (s_line_len < UART_LINE_MAX - 1U)
    {
      s_line[s_line_len] = (char)c;
      s_line_len++;
    }
  }

  (void)HAL_UART_Receive_IT(&huart1, &s_rx_byte, 1U);
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance != USART1)
  {
    return;
  }
  __HAL_UART_CLEAR_FLAG(huart,
                        (uint32_t)(UART_CLEAR_OREF | UART_CLEAR_NEF | UART_CLEAR_PEF | UART_CLEAR_FEF));
  (void)HAL_UART_Receive_IT(&huart1, &s_rx_byte, 1U);
}

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
  char *p = *ctx;
  size_t n = 0U;

  p = skip_ws(p);
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

static void cmd_help(void)
{
#if (INTAN_HW_PRESENT == 1)
  uart_tx_str(
      "OK CMDS: HELP PING ID ROM READRAW r INIT_STIM INIT_RECORD [ksps] CLEAR_ADC CLEAR_COMP\r\n"
      "  READ r | WRITE r hex [u m] | CONVERT ch [h d]\r\n"
      "  STIM_SETUP spec neg pos | STIM_ON spec [pol] | STIM_OFF [spec]\r\n"
      "  STIM_PULSE ch pos_ua hold_ms [neg_ua] | STIM_SAW spec steps max_ua period_ms cycles\r\n"
      "  BENCH n [ch] — safe 3-slot; BENCH_FAST n [ch] — pipelined, CS per slot\r\n"
      "  BENCH_TIMCS/BENCH_TIM n [ch] [target_ksps] — TIM1_CH2 drives CS PE11\r\n"
      "  BENCH_DMA n [ch] — SPI2 DMA + TIM1_CH2 CS high after every 32-bit command\r\n"
      "  ch=63 авто (per_ch=total/16)\r\n"
      "  spec: ALL,*|0,2|0-3  pol 0=neg 1=pos  h,d: DSP/DC flags\r\n");
#else
  uart_tx_str(
      "OK CMDS: HELP PING — Intan не подключён (INTAN_HW_PRESENT=0)\r\n");
#endif
}

#if (INTAN_HW_PRESENT == 0)
static int cmd_needs_intan(const char *cmd)
{
  if (strcmp(cmd, "HELP") == 0 || strcmp(cmd, "?") == 0 || strcmp(cmd, "PING") == 0)
  {
    return 0;
  }
  return 1;
}
#endif

static void dispatch_line(char *line)
{
  char *ctx = line;
  char cmd[24];
  char a[24];
  char b[24];
  char c[24];
  unsigned long u0;
  unsigned long u1;
  uint16_t mask;
  uint16_t val;
  HAL_StatusTypeDef st;

  if (next_token(&ctx, cmd, sizeof(cmd)) == 0)
  {
    return;
  }

  if (strcmp(cmd, "HELP") == 0 || strcmp(cmd, "?") == 0)
  {
    cmd_help();
    return;
  }
  if (strcmp(cmd, "PING") == 0)
  {
    reply_ok("PONG");
    return;
  }

#if (INTAN_HW_PRESENT == 0)
  if (cmd_needs_intan(cmd) != 0)
  {
    reply_err("no intan hw");
    return;
  }
#endif

  if (strcmp(cmd, "ID") == 0)
  {
    uint32_t raw32 = 0U;
    st = Intan_ReadReg_WithRaw(255U, &val, &raw32);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    {
      char msg[96];
      const char *hi = ((raw32 & 0xFFFF0000UL) != 0UL) ? "HI16_BAD" : "HI16_OK";
      (void)snprintf(msg, sizeof(msg), "CHIP 0x%04X raw32=0x%08lX %s", (unsigned int)val, (unsigned long)raw32,
                     hi);
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "ROM") == 0)
  {
    uint16_t r251, r252, r253;
    char msg[UART_TX_MAX];
    char asc[10];
    size_t ai;
    unsigned int ri;
    st = Intan_ReadReg(251U, &r251);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    st = Intan_ReadReg(252U, &r252);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    st = Intan_ReadReg(253U, &r253);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    ai = 0U;
    for (ri = 0U; ri < 3U; ri++)
    {
      uint16_t w = (ri == 0U) ? r251 : (ri == 1U ? r252 : r253);
      uint8_t lo = (uint8_t)(w & 0xFFU);
      uint8_t hi = (uint8_t)((w >> 8) & 0xFFU);
      if (lo >= 32U && lo <= 126U && ai + 1U < sizeof(asc))
      {
        asc[ai++] = (char)lo;
      }
      if (hi >= 32U && hi <= 126U && ai + 1U < sizeof(asc))
      {
        asc[ai++] = (char)hi;
      }
    }
    asc[ai] = '\0';
    (void)snprintf(msg, sizeof(msg),
                   "251=0x%04X 252=0x%04X 253=0x%04X expect INTAN ascii=\"%s\"",
                   (unsigned)r251, (unsigned)r252, (unsigned)r253, asc);
    reply_ok(msg);
    return;
  }
  if (strcmp(cmd, "READRAW") == 0)
  {
    uint32_t raw32 = 0U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 > 255U)
    {
      reply_err("reg");
      return;
    }
    st = Intan_ReadReg_WithRaw((uint8_t)u0, &val, &raw32);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    {
      char msg[96];
      const char *hi = ((raw32 & 0xFFFF0000UL) != 0UL) ? "HI16_BAD" : "HI16_OK";
      (void)snprintf(msg, sizeof(msg), "R%u 0x%04X raw32=0x%08lX %s", (unsigned)u0, (unsigned int)val,
                     (unsigned long)raw32, hi);
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "INIT_STIM") == 0)
  {
    st = Intan_App_InitStim();
    if (st != HAL_OK)
    {
      reply_err("init_stim");
      return;
    }
    reply_ok("INIT_STIM");
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
      reply_err("init_record");
      return;
    }
    {
      char msg[40];
      (void)snprintf(msg, sizeof(msg), "INIT_RECORD %u", (unsigned)ksps);
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "CLEAR_ADC") == 0)
  {
    st = Intan_App_ClearAdc();
    if (st != HAL_OK)
    {
      reply_err("clear_adc");
      return;
    }
    reply_ok("CLEAR_ADC");
    return;
  }
  if (strcmp(cmd, "CLEAR_COMP") == 0)
  {
    st = Intan_App_ClearCompliance();
    if (st != HAL_OK)
    {
      reply_err("clear_comp");
      return;
    }
    reply_ok("CLEAR_COMP");
    return;
  }
  if (strcmp(cmd, "READ") == 0)
  {
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 > 255U)
    {
      reply_err("reg");
      return;
    }
    st = Intan_ReadReg((uint8_t)u0, &val);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    {
      char msg[32];
      (void)snprintf(msg, sizeof(msg), "R%u 0x%04X", (unsigned)u0, val);
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "WRITE") == 0)
  {
    uint8_t ru = 0U;
    uint8_t rm = 0U;
    if (next_token(&ctx, a, sizeof(a)) == 0 || next_token(&ctx, b, sizeof(b)) == 0)
    {
      reply_err("args");
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
      reply_err("range");
      return;
    }
    st = Intan_WriteReg((uint8_t)u0, (uint16_t)u1, ru, rm);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    reply_ok("WRITE");
    return;
  }
  if (strcmp(cmd, "CONVERT") == 0)
  {
    uint8_t ch;
    uint8_t flags = 0U;
    uint16_t adc;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    ch = (uint8_t)strtoul(a, NULL, 0);
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      if (strtoul(b, NULL, 0) != 0UL)
      {
        flags |= 1U;
      }
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      if (strtoul(b, NULL, 0) != 0UL)
      {
        flags |= 2U;
      }
    }
    st = Intan_Convert(ch, flags, &adc);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    {
      char msg[32];
      (void)snprintf(msg, sizeof(msg), "ADC 0x%04X", adc);
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "STIM_SETUP") == 0)
  {
    if (next_token(&ctx, a, sizeof(a)) == 0 || next_token(&ctx, b, sizeof(b)) == 0 ||
        next_token(&ctx, c, sizeof(c)) == 0)
    {
      reply_err("args");
      return;
    }
    if (Intan_App_ParseChMask(a, &mask) != 0)
    {
      reply_err("ch");
      return;
    }
    u0 = strtoul(b, NULL, 0);
    u1 = strtoul(c, NULL, 0);
    st = Intan_App_StimSetupCurrents(mask, (unsigned)u0, (unsigned)u1);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    reply_ok("STIM_SETUP");
    return;
  }
  if (strcmp(cmd, "STIM_ON") == 0)
  {
    uint8_t negpol = 1U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    if (Intan_App_ParseChMask(a, &mask) != 0)
    {
      reply_err("ch");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      negpol = (uint8_t)(strtoul(b, NULL, 0) != 0UL ? 0U : 1U);
    }
    st = Intan_App_StimEnable(mask, 1U, negpol);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    reply_ok("STIM_ON");
    return;
  }
  if (strcmp(cmd, "STIM_OFF") == 0)
  {
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      st = Intan_App_StimEnable(0U, 0U, 0U);
    }
    else
    {
      if (Intan_App_ParseChMask(a, &mask) != 0)
      {
        reply_err("ch");
        return;
      }
      st = Intan_App_StimEnable(mask, 0U, 0U);
    }
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    reply_ok("STIM_OFF");
    return;
  }
  if (strcmp(cmd, "STIM_PULSE") == 0)
  {
    unsigned negua = 0U;
    uint8_t ch;
    unsigned posua;
    uint32_t hold_ms;

    if (next_token(&ctx, a, sizeof(a)) == 0 || next_token(&ctx, b, sizeof(b)) == 0 ||
        next_token(&ctx, c, sizeof(c)) == 0)
    {
      reply_err("args");
      return;
    }
    ch = (uint8_t)strtoul(a, NULL, 0);
    posua = (unsigned)strtoul(b, NULL, 0);
    hold_ms = (uint32_t)strtoul(c, NULL, 0);
    if (ch > 15U || posua > 255U)
    {
      reply_err("range");
      return;
    }
    if (next_token(&ctx, a, sizeof(a)) > 0)
    {
      negua = (unsigned)strtoul(a, NULL, 0);
    }
    mask = (uint16_t)(1U << (unsigned)ch);

    st = Intan_App_SetStimMagnitude(ch, negua, 0U);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    st = Intan_App_SetStimMagnitude(ch, posua, 1U);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }

    if (negua > 0U)
    {
      st = Intan_App_StimEnable(mask, 1U, 1U);
      if (st != HAL_OK)
      {
        reply_err("spi");
        return;
      }
      HAL_Delay(hold_ms);
      st = Intan_App_StimEnable(mask, 0U, 0U);
      if (st != HAL_OK)
      {
        reply_err("spi");
        return;
      }
    }

    st = Intan_App_StimEnable(mask, 1U, 0U);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    HAL_Delay(hold_ms);
    st = Intan_App_StimEnable(mask, 0U, 0U);
    if (st != HAL_OK)
    {
      reply_err("spi");
      return;
    }
    reply_ok("STIM_PULSE");
    return;
  }
  if (strcmp(cmd, "STIM_SAW") == 0)
  {
    char t_spec[48];
    char t_steps[16];
    char t_max[16];
    char t_per[16];
    char t_cyc[16];
    unsigned long steps;
    unsigned long maxua;
    unsigned long per_ms;
    unsigned long cycles;

    if (next_token(&ctx, t_spec, sizeof(t_spec)) == 0 || next_token(&ctx, t_steps, sizeof(t_steps)) == 0 ||
        next_token(&ctx, t_max, sizeof(t_max)) == 0 || next_token(&ctx, t_per, sizeof(t_per)) == 0 ||
        next_token(&ctx, t_cyc, sizeof(t_cyc)) == 0)
    {
      reply_err("args");
      return;
    }
    if (Intan_App_ParseChMask(t_spec, &mask) != 0)
    {
      reply_err("ch");
      return;
    }
    steps = strtoul(t_steps, NULL, 0);
    maxua = strtoul(t_max, NULL, 0);
    per_ms = strtoul(t_per, NULL, 0);
    cycles = strtoul(t_cyc, NULL, 0);
    st = Intan_App_StimSawtooth(mask, (unsigned)steps, (unsigned)maxua, (uint32_t)per_ms, (uint32_t)cycles);
    if (st != HAL_OK)
    {
      reply_err("saw");
      return;
    }
    reply_ok("STIM_SAW");
    return;
  }
  if (strcmp(cmd, "BENCH") == 0)
  {
    float kt;
    float kp;
    uint8_t ch = 63U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL || u0 > 2000000UL)
    {
      reply_err("n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      ch = (uint8_t)strtoul(b, NULL, 0);
    }
    st = Intan_App_BenchConvert((uint32_t)u0, ch, &kt, &kp);
    if (st != HAL_OK)
    {
      reply_err("bench");
      return;
    }
    {
      char msg[96];
      uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
      uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);
      (void)snprintf(msg, sizeof(msg),
                     "BENCH n=%lu ch=%u ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu",
                     u0, (unsigned)ch,
                     (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                     (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "BENCH_FAST") == 0)
  {
    float kt;
    float kp;
    uint8_t ch = 63U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL || u0 > 2000000UL)
    {
      reply_err("n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      ch = (uint8_t)strtoul(b, NULL, 0);
    }
    st = Intan_App_BenchConvertFast((uint32_t)u0, ch, &kt, &kp);
    if (st != HAL_OK)
    {
      reply_err("bench_fast");
      return;
    }
    {
      char msg[104];
      uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
      uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);
      (void)snprintf(msg, sizeof(msg),
                     "BENCH_FAST n=%lu ch=%u ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu",
                     u0, (unsigned)ch,
                     (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                     (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "BENCH_DMA") == 0)
  {
    float kt;
    float kp;
    uint8_t ch = 63U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL || u0 > 2000000UL)
    {
      reply_err("n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      ch = (uint8_t)strtoul(b, NULL, 0);
    }
    st = Intan_App_BenchConvertDmaTimCs((uint32_t)u0, ch, &kt, &kp);
    if (st != HAL_OK)
    {
      reply_err("bench_dma");
      return;
    }
    {
      char msg[104];
      uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
      uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);
      (void)snprintf(msg, sizeof(msg),
                     "BENCH_DMA n=%lu ch=%u ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu",
                     u0, (unsigned)ch,
                     (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                     (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
      reply_ok(msg);
    }
    return;
  }
  if (strcmp(cmd, "BENCH_TIMCS") == 0 || strcmp(cmd, "BENCH_TIM") == 0)
  {
    float kt;
    float kp;
    uint8_t ch = 63U;
    uint32_t target_ksps = 600U;
    if (next_token(&ctx, a, sizeof(a)) == 0)
    {
      reply_err("args");
      return;
    }
    u0 = strtoul(a, NULL, 0);
    if (u0 == 0UL || u0 > 2000000UL)
    {
      reply_err("n");
      return;
    }
    if (next_token(&ctx, b, sizeof(b)) > 0)
    {
      ch = (uint8_t)strtoul(b, NULL, 0);
    }
    if (next_token(&ctx, c, sizeof(c)) > 0)
    {
      target_ksps = (uint32_t)strtoul(c, NULL, 0);
      if (target_ksps < 100U || target_ksps > 720U)
      {
        reply_err("target");
        return;
      }
    }
    st = Intan_App_BenchConvertTimCs((uint32_t)u0, ch, target_ksps, &kt, &kp);
    if (st != HAL_OK)
    {
      reply_err("bench_timcs");
      return;
    }
    {
      char msg[120];
      uint32_t kt_milli = (uint32_t)((kt * 1000.0f) + 0.5f);
      uint32_t kp_milli = (uint32_t)((kp * 1000.0f) + 0.5f);
      (void)snprintf(msg, sizeof(msg),
                     "BENCH_TIMCS n=%lu ch=%u target=%lu ksps_total=%lu.%03lu ksps_per_ch=%lu.%03lu",
                     u0, (unsigned)ch, (unsigned long)target_ksps,
                     (unsigned long)(kt_milli / 1000UL), (unsigned long)(kt_milli % 1000UL),
                     (unsigned long)(kp_milli / 1000UL), (unsigned long)(kp_milli % 1000UL));
      reply_ok(msg);
    }
    return;
  }
  reply_err("cmd");
}

void Intan_UART_CLI_Init(void)
{
  s_line_len = 0U;
  s_line_ready = 0U;
  s_line[0] = '\0';

  Intan_App_DWT_Reset();

  HAL_NVIC_SetPriority(USART1_IRQn, 6U, 0U);
  HAL_NVIC_EnableIRQ(USART1_IRQn);

  (void)HAL_UART_Receive_IT(&huart1, &s_rx_byte, 1U);
}

void Intan_UART_CLI_Process(void)
{
  char buf[UART_LINE_MAX];

  if (s_line_ready == 0U)
  {
    return;
  }

  __disable_irq();
  s_line_ready = 0U;
  memcpy(buf, s_line, UART_LINE_MAX);
  __enable_irq();

  dispatch_line(buf);
}
