#ifndef USB_DEVICE_H
#define USB_DEVICE_H

#include "usbd_def.h"

#ifdef __cplusplus
extern "C" {
#endif

extern USBD_HandleTypeDef hUsbDeviceHS;

void USB_DEVICE_Init(void);

#ifdef __cplusplus
}
#endif

#endif /* USB_DEVICE_H */
