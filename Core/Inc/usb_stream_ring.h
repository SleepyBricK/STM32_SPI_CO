#ifndef USB_STREAM_RING_H
#define USB_STREAM_RING_H

#include "usb_stream_frame.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define USB_FRAME_COUNT       32U
#define USB_READY_FIFO_DEPTH  64U

typedef enum {
  FRAME_FREE = 0,
  FRAME_FILLING,
  FRAME_READY,
  FRAME_TX_BUSY
} UsbFrameState;

void UsbStreamRing_Reset(void);
UsbStreamFrame *UsbStreamRing_AcquireFilling(void);
/** Push frame to ready FIFO (production order). Returns 0 if FIFO full. */
uint8_t UsbStreamRing_MarkReady(UsbStreamFrame *frame);
/** Pop next ready frame (strict FIFO). NULL if empty. */
UsbStreamFrame *UsbStreamRing_PopReady(void);
/** Re-queue frame at front after failed USB TX. Returns 0 if FIFO full. */
uint8_t UsbStreamRing_PushReadyFront(UsbStreamFrame *frame);
void UsbStreamRing_MarkTxBusy(UsbStreamFrame *frame);
void UsbStreamRing_MarkFree(UsbStreamFrame *frame);
uint32_t UsbStreamRing_ReadyCount(void);
uint32_t UsbStreamRing_FreeCount(void);

#ifdef __cplusplus
}
#endif

#endif /* USB_STREAM_RING_H */
