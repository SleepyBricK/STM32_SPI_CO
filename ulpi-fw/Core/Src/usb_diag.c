#include "usb_diag.h"
#include "usart.h"
#include "usbd_conf.h"
#include "usbd_core.h"
#include "usb_device.h"
#include <stdio.h>

extern PCD_HandleTypeDef hpcd_USB_OTG_HS;
extern USBD_HandleTypeDef hUsbDeviceHS;

static uint32_t s_last_poll_ms;
static uint8_t s_last_pcd_state;
static uint8_t s_last_dev_state;
static uint8_t s_sof_seen;

void USB_Diag_Init(void)
{
  s_last_poll_ms = HAL_GetTick();
  s_last_pcd_state = 0xFFU;
  s_last_dev_state = 0xFFU;
  s_sof_seen = 0U;
  UART_Log("[usb] diag: plug USB3300 host port, watch connect/reset/SOF\r\n");
}

void USB_Diag_NotifySof(void)
{
  if (s_sof_seen == 0U)
  {
    s_sof_seen = 1U;
    UART_Log("[usb] SOF (host traffic)\r\n");
  }
}

void USB_Diag_Poll(void)
{
  uint32_t now = HAL_GetTick();
  uint8_t pcd_state;
  uint8_t dev_state;

  if ((now - s_last_poll_ms) < 2000U)
  {
    return;
  }
  s_last_poll_ms = now;

  pcd_state = (uint8_t)HAL_PCD_GetState(&hpcd_USB_OTG_HS);
  dev_state = (uint8_t)hUsbDeviceHS.dev_state;

  if ((pcd_state != s_last_pcd_state) || (dev_state != s_last_dev_state))
  {
    char line[72];
    (void)snprintf(line, sizeof(line),
                   "[usb] poll pcd=%u dev=%u sof=%u\r\n",
                   (unsigned)pcd_state, (unsigned)dev_state, (unsigned)s_sof_seen);
    UART_Log(line);
    s_last_pcd_state = pcd_state;
    s_last_dev_state = dev_state;
  }
}
