#ifndef USBD_DESC_H
#define USBD_DESC_H

#include "usbd_def.h"

#ifdef __cplusplus
extern "C" {
#endif

#define USBD_VID                         0x0483U
#define USBD_PID_HS_CDC                  0x5740U
#define USBD_LANGID_STRING               0x409U

extern USBD_DescriptorsTypeDef HS_Desc;

#ifdef __cplusplus
}
#endif

#endif /* USBD_DESC_H */
