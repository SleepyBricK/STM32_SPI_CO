#include "usb_device.h"
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_vendor_bulk.h"
#include "usbd_conf.h"
#include "usart.h"

extern PCD_HandleTypeDef hpcd_USB_OTG_HS;

USBD_HandleTypeDef hUsbDeviceHS;

void USB_DEVICE_Init(void)
{
  UART_DebugMark("[USB] USBD_Init...\r\n");
  if (USBD_Init(&hUsbDeviceHS, &HS_Desc, DEVICE_HS) != USBD_OK)
  {
    UART_DebugMark("[USB] USBD_Init failed\r\n");
    Error_Handler();
  }

  UART_DebugMark("[USB] USBD_RegisterClass...\r\n");
  if (USBD_RegisterClass(&hUsbDeviceHS, &USBD_VENDOR_BULK) != USBD_OK)
  {
    UART_DebugMark("[USB] USBD_RegisterClass failed\r\n");
    Error_Handler();
  }

  UART_DebugMark("[USB] USBD_Start...\r\n");
  if (USBD_Start(&hUsbDeviceHS) != USBD_OK)
  {
    UART_DebugMark("[USB] USBD_Start failed\r\n");
    Error_Handler();
  }

  /* После прошивки/reset хост может не увидеть attach без reconnect pulse. */
  (void)HAL_PCD_DevDisconnect(&hpcd_USB_OTG_HS);
  HAL_Delay(50);
  (void)HAL_PCD_DevConnect(&hpcd_USB_OTG_HS);

  UART_DebugMark("[USB] HS vendor bulk started\r\n");
  USBD_LogStatus("started");
}

void USB_DEVICE_FinalizeAttach(void)
{
  /*
   * Пульс после полного init: при прошивке через ST-Link хост часто не видит attach,
   * если USB3300 уже был подключён до USBD_Start.
   */
  UART_DebugMark("[USB] late DevDisconnect/DevConnect (re-enumerate)\r\n");
  (void)HAL_PCD_DevDisconnect(&hpcd_USB_OTG_HS);
  HAL_Delay(50);
  (void)HAL_PCD_DevConnect(&hpcd_USB_OTG_HS);
  HAL_Delay(100);
  USBD_LogStatus("late attach");
}

void USB_DEVICE_PollEvents(void)
{
  static uint32_t s_last_rst;
  static uint32_t s_last_conn;
  static uint32_t s_last_disc;
  static uint8_t s_last_state;

  if (g_usb_ev_reset != s_last_rst)
  {
    s_last_rst = g_usb_ev_reset;
    USBD_LogStatus("host reset");
  }
  if (g_usb_ev_connect != s_last_conn)
  {
    s_last_conn = g_usb_ev_connect;
    USBD_LogStatus("connect");
  }
  if (g_usb_ev_disconnect != s_last_disc)
  {
    s_last_disc = g_usb_ev_disconnect;
    USBD_LogStatus("disconnect");
  }
  if (hUsbDeviceHS.dev_state != s_last_state)
  {
    s_last_state = hUsbDeviceHS.dev_state;
    USBD_LogStatus("state chg");
  }
}
