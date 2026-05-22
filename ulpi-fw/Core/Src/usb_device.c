#include "usb_device.h"
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_vendor_bulk.h"
#include "usart.h"

USBD_HandleTypeDef hUsbDeviceHS;

void USB_DEVICE_Init(void)
{
  if (USBD_Init(&hUsbDeviceHS, &HS_Desc, DEVICE_HS) != USBD_OK)
  {
    UART_Log("[usb] USBD_Init fail\r\n");
    Error_Handler();
  }
  if (USBD_RegisterClass(&hUsbDeviceHS, &USBD_VENDOR_BULK) != USBD_OK)
  {
    UART_Log("[usb] RegisterClass fail\r\n");
    Error_Handler();
  }
  if (USBD_Start(&hUsbDeviceHS) != USBD_OK)
  {
    UART_Log("[usb] USBD_Start fail\r\n");
    Error_Handler();
  }
}
