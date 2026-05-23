#include "usb_device.h"
#include "main.h"
#include "usbd_core.h"
#include "usbd_desc.h"
#include "usb_vendor_bulk.h"
#include "usbd_conf.h"

extern PCD_HandleTypeDef hpcd_USB_OTG_HS;

USBD_HandleTypeDef hUsbDeviceHS;

void USB_DEVICE_Init(void)
{
  if (USBD_Init(&hUsbDeviceHS, &HS_Desc, DEVICE_HS) != USBD_OK)
  {
    Error_Handler();
  }

  if (USBD_RegisterClass(&hUsbDeviceHS, &USBD_VENDOR_BULK) != USBD_OK)
  {
    Error_Handler();
  }

  if (USBD_Start(&hUsbDeviceHS) != USBD_OK)
  {
    Error_Handler();
  }

  (void)HAL_PCD_DevDisconnect(&hpcd_USB_OTG_HS);
  HAL_Delay(50);
  (void)HAL_PCD_DevConnect(&hpcd_USB_OTG_HS);
}
