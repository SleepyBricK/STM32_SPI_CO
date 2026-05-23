#include "usb_stream_frame.h"
#include <stddef.h>

_Static_assert(sizeof(UsbStreamFrame) == USB_STREAM_FRAME_SIZE, "UsbStreamFrame must be 4096 bytes");

void UsbStreamFrame_InitHeader(UsbStreamFrame *f, uint32_t frame_seq, uint32_t first_sc,
                               uint32_t spi_ovf, uint32_t usb_ovf)
{
  if (f == NULL)
  {
    return;
  }

  f->magic = USB_STREAM_FRAME_MAGIC;
  f->version = USB_STREAM_FRAME_VERSION;
  f->flags = 0U;
  f->frame_seq = frame_seq;
  f->first_sample_counter = first_sc;
  f->sample_count = 0U;
  f->spi_overflow_count = spi_ovf;
  f->usb_overflow_count = usb_ovf;
  f->reserved = 0U;
}

void UsbStreamFrame_FillSynth(UsbStreamFrame *f, uint32_t frame_seq, uint32_t first_sc,
                              uint32_t count, uint32_t spi_ovf, uint32_t usb_ovf)
{
  uint32_t i;

  if (f == NULL)
  {
    return;
  }

  if (count > USB_STREAM_FRAME_RESPONSES)
  {
    count = USB_STREAM_FRAME_RESPONSES;
  }

  UsbStreamFrame_InitHeader(f, frame_seq, first_sc, spi_ovf, usb_ovf);
  f->sample_count = count;

  for (i = 0U; i < count; i++)
  {
    f->response[i] = (uint16_t)((first_sc + i) & 0xFFFFU);
  }

  for (i = count; i < USB_STREAM_FRAME_RESPONSES; i++)
  {
    f->response[i] = 0U;
  }
}

int UsbStreamFrame_VerifySynth(const UsbStreamFrame *f)
{
  static const uint32_t idxs[] = {0U, 1U, 239U, 240U, 241U, 496U, 1007U, 1008U, 1264U, 2031U};
  unsigned k;

  if (f == NULL)
  {
    return -1;
  }

  if (f->magic != USB_STREAM_FRAME_MAGIC)
  {
    return -1;
  }
  if (f->version != USB_STREAM_FRAME_VERSION)
  {
    return -2;
  }
  if (f->sample_count > USB_STREAM_FRAME_RESPONSES)
  {
    return -3;
  }

  for (k = 0U; k < (sizeof(idxs) / sizeof(idxs[0])); k++)
  {
    uint32_t i = idxs[k];
    uint16_t want;

    if (i >= f->sample_count)
    {
      continue;
    }

    want = (uint16_t)((f->first_sample_counter + i) & 0xFFFFU);
    if (f->response[i] != want)
    {
      return -10;
    }
  }

  return 0;
}
