#include "main.h"
#include "usb_device.h"
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_cdc.h"
#include "usbd_cdc_if.h"
#include "uart_diag.h"

extern PCD_HandleTypeDef hpcd_USB_OTG_HS;

USBD_HandleTypeDef hUsbDeviceHS;

static const char *USBD_StatusStr(uint8_t st)
{
  switch (st)
  {
    case USBD_OK:   return "OK";
    case USBD_BUSY: return "BUSY";
    case USBD_FAIL: return "FAIL";
    default:        return "?";
  }
}

void USB_DEVICE_Init(void)
{
  uint8_t st;

  UART_DiagMark("[USB] === USB_DEVICE_Init start ===\r\n");

  st = USBD_Init(&hUsbDeviceHS, &HS_Desc, DEVICE_HS);
  UART_DiagPrintf("[USB] USBD_Init -> %s (%u)\r\n", USBD_StatusStr(st), (unsigned)st);
  if (st != USBD_OK) { Error_Handler(); }
  UART_DiagDumpUsbd("after Init");

  st = USBD_RegisterClass(&hUsbDeviceHS, USBD_CDC_CLASS);
  UART_DiagPrintf("[USB] USBD_RegisterClass(CDC) -> %s\r\n", USBD_StatusStr(st));
  if (st != USBD_OK) { Error_Handler(); }

  st = USBD_CDC_RegisterInterface(&hUsbDeviceHS, &USBD_CDC_fops);
  UART_DiagPrintf("[USB] USBD_CDC_RegisterInterface -> %s\r\n", USBD_StatusStr(st));
  if (st != USBD_OK) { Error_Handler(); }

  st = USBD_Start(&hUsbDeviceHS);
  UART_DiagPrintf("[USB] USBD_Start -> %s\r\n", USBD_StatusStr(st));
  if (st != USBD_OK) { Error_Handler(); }

  UART_DiagDumpPcd("after Start");
  UART_DiagDumpOtgHsRegs();

  UART_DiagMark("[USB] DevDisconnect 50ms DevConnect (host re-enumerate)\r\n");
  (void)HAL_PCD_DevDisconnect(&hpcd_USB_OTG_HS);
  HAL_Delay(50);
  (void)HAL_PCD_DevConnect(&hpcd_USB_OTG_HS);

  UART_DiagDumpPcd("after reconnect");
  UART_DiagDumpUsbd("running");
  UART_DiagMark("[USB] === USB_DEVICE_Init done ===\r\n");
  UART_DiagMark("[USB] Mac: unplug/replug USB, ls /dev/cu.usbmodem*\r\n");
}
