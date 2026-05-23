#ifndef USB_STREAM_SERVICE_H
#define USB_STREAM_SERVICE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint32_t samples_produced;
  uint32_t frames_produced;
  uint32_t frames_sent;
  uint32_t usb_overflow_count;
  uint32_t spi_overflow_count;
  uint32_t usb_tx_errors;
  uint32_t spi_xfer32_count;
  uint32_t responses_pushed;
} UsbStreamStats;

void UsbStreamService_Init(void);
void UsbVendorBulk_ProcessOutCommands(void);
void UsbStreamService_Process(void);
void UsbStreamService_TxPump(void);
const UsbStreamStats *UsbStreamService_GetStats(void);

void UsbStreamService_NoteSample(void);
void UsbStreamService_NoteSamples(uint32_t count);
void UsbStreamService_NoteFrameProduced(void);
void UsbStreamService_NoteUsbOverflow(void);
void UsbStreamService_NoteSpiOverflow(void);
uint32_t UsbStreamService_GetSpiOverflowCount(void);
uint32_t UsbStreamService_GetUsbOverflowCount(void);

#ifdef __cplusplus
}
#endif

#endif /* USB_STREAM_SERVICE_H */
