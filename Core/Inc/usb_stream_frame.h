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

#define USB_STREAM_FLAG_COUNTER          0x0001U
#define USB_STREAM_FLAG_REAL_ADC         0x0002U
#define USB_STREAM_FLAG_RR               0x0004U

#define USB_STREAM_META(first_channel, channel_count, convert_flags) \
  ((((uint32_t)(first_channel) & 0xFFU) << 0) | \
   (((uint32_t)(channel_count) & 0xFFU) << 8) | \
   (((uint32_t)(convert_flags) & 0xFFU) << 16))
#define USB_STREAM_META_FIRST_CHANNEL(meta)  ((uint8_t)(((uint32_t)(meta) >> 0) & 0xFFU))
#define USB_STREAM_META_CHANNEL_COUNT(meta)  ((uint8_t)(((uint32_t)(meta) >> 8) & 0xFFU))
#define USB_STREAM_META_CONVERT_FLAGS(meta)  ((uint8_t)(((uint32_t)(meta) >> 16) & 0xFFU))

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
