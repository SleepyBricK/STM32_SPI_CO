#include "intan_stream.h"
#include "usb_stream_frame.h"
#include "usb_stream_ring.h"
#include "usb_stream_service.h"
#include "intan_spi.h"
#include <stddef.h>
#include <string.h>

static UsbStreamFrame *s_cur;
static uint32_t s_cur_pos;
static uint32_t s_frame_seq;
static uint32_t s_next_sample;
static uint32_t s_hdr_spi_ovf;
static uint32_t s_hdr_usb_ovf;
static uint16_t s_frame_flags;
static uint32_t s_stream_meta;
static uint8_t s_active;

static void intan_stream_open_frame(void)
{
  s_cur = UsbStreamRing_AcquireFilling();
  if (s_cur == NULL)
  {
    UsbStreamService_NoteUsbOverflow();
    return;
  }

  s_hdr_spi_ovf = UsbStreamService_GetSpiOverflowCount();
  s_hdr_usb_ovf = UsbStreamService_GetUsbOverflowCount();
  UsbStreamFrame_InitHeader(s_cur, s_frame_seq, s_next_sample, s_hdr_spi_ovf, s_hdr_usb_ovf);
  s_cur->flags = s_frame_flags;
  s_cur->reserved = s_stream_meta;
  s_frame_seq++;
  s_cur_pos = 0U;
}

static void intan_stream_finalize_frame(void)
{
  if (s_cur == NULL || s_cur_pos == 0U)
  {
    s_cur = NULL;
    s_cur_pos = 0U;
    return;
  }

  s_cur->sample_count = s_cur_pos;
  s_cur->spi_overflow_count = s_hdr_spi_ovf;
  s_cur->usb_overflow_count = s_hdr_usb_ovf;

  if (UsbStreamRing_MarkReady(s_cur) == 0U)
  {
    UsbStreamService_NoteUsbOverflow();
    UsbStreamService_NoteSamplesDropped(s_cur_pos);
    UsbStreamRing_MarkFree(s_cur);
  }
  else
  {
    UsbStreamService_NoteFrameProduced();
  }

  s_cur = NULL;
  s_cur_pos = 0U;
}

static uint32_t intan_stream_max_samples(void)
{
  if ((s_frame_flags & USB_STREAM_FLAG_CHANNEL_TAG) != 0U)
  {
    return USB_STREAM_FRAME_TAGGED_SAMPLES;
  }

  return USB_STREAM_FRAME_RESPONSES;
}

static void intan_stream_append_samples(const uint16_t *src, uint32_t count, uint32_t counter_base)
{
  uint32_t off = 0U;
  uint32_t max_samples = intan_stream_max_samples();

  while (off < count)
  {
    uint32_t room;
    uint32_t n;

    if (s_cur == NULL)
    {
      intan_stream_open_frame();
      if (s_cur == NULL)
      {
        UsbStreamService_NoteUsbOverflow();
        UsbStreamService_NoteSamplesDropped(count - off);
        return;
      }
    }

    room = max_samples - s_cur_pos;
    n = count - off;
    if (n > room)
    {
      n = room;
    }

    if (src != NULL)
    {
      memcpy(&s_cur->response[s_cur_pos], &src[off], n * sizeof(uint16_t));
    }
    else
    {
      uint32_t i;
      for (i = 0U; i < n; i++)
      {
        s_cur->response[s_cur_pos + i] = (uint16_t)((counter_base + off + i) & 0xFFFFU);
      }
    }

    s_cur_pos += n;
    s_next_sample += n;
    off += n;

    if (s_cur_pos >= max_samples)
    {
      intan_stream_finalize_frame();
    }
  }

  UsbStreamService_NoteSamples(count);
}

static void intan_stream_append_tagged_from_adc(const uint16_t *adc, uint32_t count,
                                                uint8_t first_channel, uint8_t channel_count,
                                                uint8_t phase, uint32_t rx_offset)
{
  uint32_t off = 0U;

  if ((s_frame_flags & USB_STREAM_FLAG_CHANNEL_TAG) == 0U || adc == NULL || count == 0U)
  {
    return;
  }

  while (off < count)
  {
    uint32_t room;
    uint32_t n;
    uint32_t i;
    uint32_t *dst;

    if (s_cur == NULL)
    {
      intan_stream_open_frame();
      if (s_cur == NULL)
      {
        UsbStreamService_NoteUsbOverflow();
        UsbStreamService_NoteSamplesDropped(count - off);
        return;
      }
    }

    room = USB_STREAM_FRAME_TAGGED_SAMPLES - s_cur_pos;
    n = count - off;
    if (n > room)
    {
      n = room;
    }

    dst = UsbStreamFrame_TaggedPayload(s_cur);
    for (i = 0U; i < n; i++)
    {
      uint8_t ch = first_channel;

      if (channel_count > 1U)
      {
        ch = (uint8_t)(first_channel +
                       Intan_PipelineChannelIndex(phase, off + i, rx_offset, channel_count));
      }

      dst[s_cur_pos + i] = USB_STREAM_MAKE_TAGGED_WORD(ch, adc[off + i]);
    }

    s_cur_pos += n;
    s_next_sample += n;
    off += n;

    if (s_cur_pos >= USB_STREAM_FRAME_TAGGED_SAMPLES)
    {
      intan_stream_finalize_frame();
    }
  }

  UsbStreamService_NoteSamples(count);
}

void IntanStream_Reset(void)
{
  intan_stream_finalize_frame();
  s_frame_seq = 0U;
  s_next_sample = 0U;
  s_frame_flags = 0U;
  s_stream_meta = 0U;
  s_active = 0U;
}

void IntanStream_Begin(void)
{
  IntanStream_BeginWithMeta(0U, 0U);
}

void IntanStream_BeginWithMeta(uint16_t frame_flags, uint32_t stream_meta)
{
  s_cur = NULL;
  s_cur_pos = 0U;
  s_frame_seq = 0U;
  s_next_sample = 0U;
  s_frame_flags = frame_flags;
  s_stream_meta = stream_meta;
  s_active = 1U;
}

void IntanStream_End(void)
{
  intan_stream_finalize_frame();
  s_active = 0U;
}

void IntanStream_PushResponse(uint16_t response)
{
  if (s_active == 0U)
  {
    return;
  }

  intan_stream_append_samples(&response, 1U, 0U);
}

void IntanStream_PushCounterBlock(uint32_t base, uint32_t count)
{
  if (s_active == 0U || count == 0U)
  {
    return;
  }

  intan_stream_append_samples(NULL, count, base);
}

void IntanStream_PushBlock(const uint16_t *src, uint32_t count)
{
  if (s_active == 0U || src == NULL || count == 0U)
  {
    return;
  }

  intan_stream_append_samples(src, count, 0U);
}

void IntanStream_PushBlockTaggedFromAdc(const uint16_t *adc, uint32_t count, uint8_t first_channel,
                                        uint8_t channel_count, uint8_t phase, uint32_t rx_offset)
{
  if (s_active == 0U || adc == NULL || count == 0U)
  {
    return;
  }

  intan_stream_append_tagged_from_adc(adc, count, first_channel, channel_count, phase, rx_offset);
}

void IntanStream_PushFwSequence16(const uint16_t adc_by_channel[16])
{
  uint32_t i;
  uint32_t *dst;

  if (s_active == 0U || adc_by_channel == NULL ||
      (s_frame_flags & USB_STREAM_FLAG_CHANNEL_TAG) == 0U)
  {
    return;
  }

  if (s_cur == NULL)
  {
    intan_stream_open_frame();
    if (s_cur == NULL)
    {
      UsbStreamService_NoteUsbOverflow();
      UsbStreamService_NoteSamplesDropped(16U);
      return;
    }
  }

  if ((USB_STREAM_FRAME_TAGGED_SAMPLES - s_cur_pos) < 16U)
  {
    intan_stream_finalize_frame();
    intan_stream_open_frame();
    if (s_cur == NULL)
    {
      UsbStreamService_NoteUsbOverflow();
      UsbStreamService_NoteSamplesDropped(16U);
      return;
    }
  }

  dst = UsbStreamFrame_TaggedPayload(s_cur);
  for (i = 0U; i < 16U; i++)
  {
    dst[s_cur_pos + i] = USB_STREAM_MAKE_TAGGED_WORD((uint8_t)i, adc_by_channel[i]);
  }

  s_cur_pos += 16U;
  s_next_sample += 16U;

  if (s_cur_pos >= USB_STREAM_FRAME_TAGGED_SAMPLES)
  {
    intan_stream_finalize_frame();
  }

  UsbStreamService_NoteSamples(16U);
}

uint32_t IntanStream_PeekNextSample(void)
{
  return s_next_sample;
}

uint8_t IntanStream_IsActive(void)
{
  return s_active;
}
