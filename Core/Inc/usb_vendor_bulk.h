#ifndef USB_VENDOR_BULK_H
#define USB_VENDOR_BULK_H

#include "usbd_conf.h"
#include "usbd_ioreq.h"
#include "usb_stream_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

#define VENDOR_BULK_IN_EP                0x81U
#define VENDOR_BULK_OUT_EP               0x01U
#define VENDOR_BULK_HS_MAX_PACKET        512U
#define VENDOR_BULK_FS_MAX_PACKET        64U

#if (USB_DEBUG_FORCE_FULL_SPEED == 1U)
#define VENDOR_BULK_MAX_PACKET           VENDOR_BULK_FS_MAX_PACKET
#else
#define VENDOR_BULK_MAX_PACKET           VENDOR_BULK_HS_MAX_PACKET
#endif

extern USBD_ClassTypeDef USBD_VENDOR_BULK;

typedef void (*USBD_VENDOR_BULK_TxCompleteFn)(uint32_t len);

uint8_t USBD_VENDOR_BULK_Transmit(uint8_t *buf, uint16_t len);
uint8_t USBD_VENDOR_BULK_TransmitFrame(const uint8_t *buf, uint32_t len);
/** Cancel an in-flight stream frame before its backing buffer is reused. */
void USBD_VENDOR_BULK_AbortFrame(void);
void USBD_VENDOR_BULK_SetTxCompleteCallback(USBD_VENDOR_BULK_TxCompleteFn cb);
uint8_t USBD_VENDOR_BULK_TxIdle(void);
uint8_t USBD_VENDOR_BULK_PollRx(uint8_t *buf, uint16_t max_len, uint16_t *len_out);
/** Number of OUT command packets rejected because the static queue was full. */
uint32_t USBD_VENDOR_BULK_GetRxOverflowCount(void);

#ifdef __cplusplus
}
#endif

#endif /* USB_VENDOR_BULK_H */
