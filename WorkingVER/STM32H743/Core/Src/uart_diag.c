#include "uart_diag.h"
#include "usart.h"
#include "usbd_core.h"
#include "usbd_conf.h"
#include "usb_device.h"
#include "usbd_desc.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#ifndef USB_VDD33_BYPASS
#define USB_VDD33_BYPASS  1U
#endif

#ifndef UART_DIAG_TX_BUF
#define UART_DIAG_TX_BUF  384U
#endif

void UART_DiagMark(const char *line)
{
  UART_DebugMark(line);
}

void UART_DiagPrintf(const char *fmt, ...)
{
  char buf[UART_DIAG_TX_BUF];
  va_list ap;
  int n;

  if (fmt == NULL)
  {
    return;
  }

  va_start(ap, fmt);
  n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);

  if (n <= 0)
  {
    return;
  }

  UART_DebugMark(buf);
}

void UART_DiagBanner(void)
{
  UART_DiagMark("\r\n");
  UART_DiagMark("========================================\r\n");
  UART_DiagMark(" STM32H743 UART diagnostic log\r\n");
  UART_DiagMark(" DEBUG UART: USART1 PB6=TX @ 115200\r\n");
  UART_DiagMark("  -> /dev/cu.usbserial-* (FTDI adapter)\r\n");
  UART_DiagMark(" USB CDC:    separate Mac port\r\n");
  UART_DiagMark("  -> /dev/cu.usbmodem* (STM32 device)\r\n");
  UART_DiagMark("========================================\r\n");
}

void UART_DiagDumpResetCause(void)
{
  uint32_t rsr = RCC->RSR;
  uint32_t csr = RCC->CSR;

  UART_DiagPrintf("[RST] tick=%lu ms\r\n", (unsigned long)HAL_GetTick());
  UART_DiagPrintf("[RST] RCC_RSR=0x%08lX RCC_CSR=0x%08lX\r\n",
                  (unsigned long)rsr, (unsigned long)csr);

  if (rsr & RCC_RSR_PINRSTF)   { UART_DiagMark("[RST]  PIN reset\r\n"); }
  if (rsr & RCC_RSR_BORRSTF)   { UART_DiagMark("[RST]  BOR reset\r\n"); }
  if (rsr & RCC_RSR_SFTRSTF)   { UART_DiagMark("[RST]  Software reset\r\n"); }
  if (rsr & RCC_RSR_IWDG1RSTF) { UART_DiagMark("[RST]  IWDG1 reset\r\n"); }
  if (rsr & RCC_RSR_WWDG1RSTF) { UART_DiagMark("[RST]  WWDG1 reset\r\n"); }
  if (rsr & RCC_RSR_LPWRRSTF)  { UART_DiagMark("[RST]  Low-power reset\r\n"); }

  __HAL_RCC_CLEAR_RESET_FLAGS();
}

void UART_DiagDumpClocks(void)
{
  UART_DiagPrintf("[CLK] SYS=%lu Hz\r\n", (unsigned long)HAL_RCC_GetSysClockFreq());
  UART_DiagPrintf("[CLK] HCLK=%lu Hz\r\n", (unsigned long)HAL_RCC_GetHCLKFreq());
  UART_DiagPrintf("[CLK] PCLK1=%lu Hz\r\n", (unsigned long)HAL_RCC_GetPCLK1Freq());
  UART_DiagPrintf("[CLK] PCLK2=%lu Hz\r\n", (unsigned long)HAL_RCC_GetPCLK2Freq());

  UART_DiagPrintf("[CLK] HSE ready=%u LSE ready=%u HSI ready=%u\r\n",
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_HSERDY),
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_LSERDY),
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_HSIRDY));
  UART_DiagPrintf("[CLK] PLL1 ready=%u PLL2 ready=%u PLL3 ready=%u\r\n",
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_PLLRDY),
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_PLL2RDY),
                  (unsigned)__HAL_RCC_GET_FLAG(RCC_FLAG_PLL3RDY));

  UART_DiagPrintf("[CLK] FLASH latency=%lu wait-states\r\n",
                  (unsigned long)FLASH->ACR & FLASH_ACR_LATENCY);
  UART_DiagPrintf("[CLK] PWR VOSRDY=%u scale config done\r\n",
                  (unsigned)__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY));
}

void UART_DiagDumpPwrUsb(void)
{
  UART_DiagPrintf("[PWR] USB33RDY=%u (1=USB 3.3V domain ready)\r\n",
                  (unsigned)__HAL_PWR_GET_FLAG(PWR_FLAG_USB33RDY));
#if (USB_VDD33_BYPASS == 1U)
  UART_DiagMark("[PWR] USB policy: VDD33 bypass (no internal USB LDO)\r\n");
#else
  UART_DiagMark("[PWR] USB policy: internal USB LDO enabled\r\n");
#endif
}

void UART_DiagDumpRccUsbPll3(void)
{
  uint32_t pllckcfg = RCC->PLLCKSELR;
  uint32_t pll3div = RCC->PLL3DIVR;
  uint32_t d2ccip2r = RCC->D2CCIP2R;

  UART_DiagPrintf("[RCC] PLLCKSELR=0x%08lX\r\n", (unsigned long)pllckcfg);
  UART_DiagPrintf("[RCC] PLL3 DIVM3=%lu N3=%lu P3=%lu Q3=%lu R3=%lu (DIVR=0x%08lX)\r\n",
                  (unsigned long)((pllckcfg & RCC_PLLCKSELR_DIVM3) >> RCC_PLLCKSELR_DIVM3_Pos),
                  (unsigned long)((pll3div & RCC_PLL3DIVR_N3) >> RCC_PLL3DIVR_N3_Pos),
                  (unsigned long)((pll3div & RCC_PLL3DIVR_P3) >> RCC_PLL3DIVR_P3_Pos),
                  (unsigned long)((pll3div & RCC_PLL3DIVR_Q3) >> RCC_PLL3DIVR_Q3_Pos),
                  (unsigned long)((pll3div & RCC_PLL3DIVR_R3) >> RCC_PLL3DIVR_R3_Pos),
                  (unsigned long)pll3div);
  {
    uint32_t usbsel = (d2ccip2r & RCC_D2CCIP2R_USBSEL) >> RCC_D2CCIP2R_USBSEL_Pos;
    const char *usbsel_name = "unknown";

    if (usbsel == 0U)      { usbsel_name = "PLL1_Q"; }
    else if (usbsel == 1U) { usbsel_name = "reserved"; }
    else if (usbsel == 2U) { usbsel_name = "PLL3_Q (OK for ULPI)"; }
    else if (usbsel == 3U) { usbsel_name = "HSI48"; }

    UART_DiagPrintf("[RCC] D2CCIP2R=0x%08lX USBSEL=%lu -> %s\r\n",
                    (unsigned long)d2ccip2r, (unsigned long)usbsel, usbsel_name);
  }
  UART_DiagPrintf("[RCC] target USB clock 48 MHz: HSE/(M3)*N3/Q3 = 8/4*96/4\r\n");
}

void UART_DiagDumpOtgHsRegs(void)
{
  USB_OTG_GlobalTypeDef *usb = USB_OTG_HS;
  uint32_t gccfg = usb->GCCFG;
  uint32_t gotgctl = usb->GOTGCTL;
  uint32_t gintsts = usb->GINTSTS;
  uint32_t gintmsk = usb->GINTMSK;

  UART_DiagPrintf("[OTG] instance=USB_OTG_HS @0x%08lX\r\n", (unsigned long)USB_OTG_HS_PERIPH_BASE);
  UART_DiagPrintf("[OTG] GCCFG=0x%08lX PWRDWN=%u VBDEN=%u\r\n",
                  (unsigned long)gccfg,
                  (unsigned)((gccfg & USB_OTG_GCCFG_PWRDWN) != 0U),
                  (unsigned)((gccfg & USB_OTG_GCCFG_VBDEN) != 0U));
  UART_DiagPrintf("[OTG] GOTGCTL=0x%08lX BSESVLD=%u\r\n",
                  (unsigned long)gotgctl,
                  (unsigned)((gotgctl & USB_OTG_GOTGCTL_BSESVLD) != 0U));
  UART_DiagPrintf("[OTG] GINTSTS=0x%08lX GINTMSK=0x%08lX\r\n",
                  (unsigned long)gintsts, (unsigned long)gintmsk);

  if (gintsts & USB_OTG_GINTSTS_CMOD)   { UART_DiagMark("[OTG]  GINT: host mode\r\n"); }
  else                                  { UART_DiagMark("[OTG]  GINT: device mode\r\n"); }
  if (gintsts & USB_OTG_GINTSTS_SOF)    { UART_DiagMark("[OTG]  GINT: SOF\r\n"); }
  if (gintsts & USB_OTG_GINTSTS_USBSUSP) { UART_DiagMark("[OTG]  GINT: USB suspend\r\n"); }
  if (gintsts & USB_OTG_GINTSTS_USBRST) { UART_DiagMark("[OTG]  GINT: USB reset\r\n"); }
  if (gintsts & USB_OTG_GINTSTS_ENUMDNE) { UART_DiagMark("[OTG]  GINT: enumeration done\r\n"); }
  if (gintsts & USB_OTG_GINTSTS_WKUINT) { UART_DiagMark("[OTG]  GINT: wakeup\r\n"); }

  UART_DiagPrintf("[OTG] AHB1ENR USB1HS=%u ULPI=%u\r\n",
                  (unsigned)((RCC->AHB1ENR & RCC_AHB1ENR_USB1OTGHSEN) != 0U),
                  (unsigned)((RCC->AHB1ENR & RCC_AHB1ENR_USB1OTGHSULPIEN) != 0U));
}

void UART_DiagDumpPcd(const char *tag)
{
  const char *lbl = (tag != NULL) ? tag : "PCD";

  UART_DiagPrintf("[%s] State=%u (0=RESET 1=READY 2=ERROR)\r\n",
                  lbl, (unsigned)hpcd_USB_OTG_HS.State);
  UART_DiagPrintf("[%s] endpoints=%u speed_cfg=%lu (0=HS 1=HS_IN_FULL)\r\n",
                  lbl,
                  (unsigned)hpcd_USB_OTG_HS.Init.dev_endpoints,
                  (unsigned long)hpcd_USB_OTG_HS.Init.speed);
  UART_DiagPrintf("[%s] phy_itface=%lu vbus_sense=%lu\r\n",
                  lbl,
                  (unsigned long)hpcd_USB_OTG_HS.Init.phy_itface,
                  (unsigned long)hpcd_USB_OTG_HS.Init.vbus_sensing_enable);
  UART_DiagPrintf("[%s] USB_Address=%u\r\n",
                  lbl, (unsigned)hpcd_USB_OTG_HS.USB_Address);
}

void UART_DiagDumpUsbd(const char *tag)
{
  const char *lbl = (tag != NULL) ? tag : "USBD";
  const char *state_name = "?";
  const char *speed_name = "?";

  switch (hUsbDeviceHS.dev_state)
  {
    case 1U:  state_name = "DEFAULT"; break;
    case 2U:  state_name = "ADDRESSED"; break;
    case 3U:  state_name = "CONFIGURED"; break;
    case 4U:  state_name = "SUSPENDED"; break;
    default:  break;
  }

  switch (hUsbDeviceHS.dev_speed)
  {
    case 0U:  speed_name = "HIGH"; break;
    case 1U:  speed_name = "FULL"; break;
    case 2U:  speed_name = "LOW"; break;
    default:  break;
  }

  UART_DiagPrintf("[%s] dev_state=%u (%s) speed=%u (%s) config=%u\r\n",
                  lbl,
                  (unsigned)hUsbDeviceHS.dev_state, state_name,
                  (unsigned)hUsbDeviceHS.dev_speed, speed_name,
                  (unsigned)hUsbDeviceHS.dev_config);
  UART_DiagPrintf("[%s] VID=0x%04X PID=0x%04X (CDC expect 0483:5740)\r\n",
                  lbl, (unsigned)USBD_VID, (unsigned)USBD_PID_HS_CDC);

  if (hUsbDeviceHS.dev_state == 3U)
  {
    UART_DiagPrintf("[%s] *** USB CONFIGURED on MCU — host enumeration OK ***\r\n", lbl);
  }
}
