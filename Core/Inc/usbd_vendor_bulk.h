#ifndef USBD_VENDOR_BULK_H
#define USBD_VENDOR_BULK_H

#include "usbd_ioreq.h"

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

/** Copy into internal TX pool (text replies). */
uint8_t USBD_VENDOR_BULK_Transmit(uint8_t *buf, uint16_t len);
/** Zero-copy: buf must stay valid until packet is fully sent. */
uint8_t USBD_VENDOR_BULK_TransmitZc(const uint8_t *buf, uint16_t len);
uint8_t USBD_VENDOR_BULK_TxReady(void);
uint8_t USBD_VENDOR_BULK_TxIdle(void);
uint8_t USBD_VENDOR_BULK_PollRx(uint8_t *buf, uint16_t max_len, uint16_t *len_out);

#ifdef __cplusplus
}
#endif

#endif /* USBD_VENDOR_BULK_H */
