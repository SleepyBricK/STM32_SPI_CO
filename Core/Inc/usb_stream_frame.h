#ifndef USB_STREAM_FRAME_H
#define USB_STREAM_FRAME_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define USB_STREAM_FRAME_MAGIC           0x52485331U /* 'RHS1' */
#define USB_STREAM_FRAME_VERSION         1U
#define USB_STREAM_FRAME_SIZE            4096U
#define USB_STREAM_HEADER_SIZE           32U
#define USB_STREAM_FRAME_RESPONSES       2032U

#pragma pack(push, 1)
typedef struct __attribute__((aligned(32))) {
  uint32_t magic;
  uint16_t version;
  uint16_t flags;
  uint32_t frame_seq;
  uint32_t first_sample_counter;
  uint32_t sample_count;
  uint32_t spi_overflow_count;
  uint32_t usb_overflow_count;
  uint32_t reserved;
  uint16_t response[USB_STREAM_FRAME_RESPONSES];
} UsbStreamFrame;
#pragma pack(pop)

void UsbStreamFrame_InitHeader(UsbStreamFrame *f, uint32_t frame_seq, uint32_t first_sc,
                               uint32_t spi_ovf, uint32_t usb_ovf);
void UsbStreamFrame_FillSynth(UsbStreamFrame *f, uint32_t frame_seq, uint32_t first_sc,
                              uint32_t count, uint32_t spi_ovf, uint32_t usb_ovf);
int UsbStreamFrame_VerifySynth(const UsbStreamFrame *f);

#ifdef __cplusplus
}
#endif

#endif /* USB_STREAM_FRAME_H */
