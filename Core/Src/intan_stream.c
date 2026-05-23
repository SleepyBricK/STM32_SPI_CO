#include "intan_stream.h"
#include "usb_stream_frame.h"
#include "usb_stream_ring.h"
#include "usb_stream_service.h"
#include <stddef.h>

static UsbStreamFrame *s_cur;
static uint32_t s_cur_pos;
static uint32_t s_frame_seq;
static uint32_t s_next_sample;
static uint32_t s_hdr_spi_ovf;
static uint32_t s_hdr_usb_ovf;
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
    UsbStreamRing_MarkFree(s_cur);
  }
  else
  {
    UsbStreamService_NoteFrameProduced();
  }

  s_cur = NULL;
  s_cur_pos = 0U;
}

void IntanStream_Reset(void)
{
  intan_stream_finalize_frame();
  s_frame_seq = 0U;
  s_next_sample = 0U;
  s_active = 0U;
}

void IntanStream_Begin(void)
{
  s_cur = NULL;
  s_cur_pos = 0U;
  s_frame_seq = 0U;
  s_next_sample = 0U;
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

  if (s_cur == NULL)
  {
    intan_stream_open_frame();
    if (s_cur == NULL)
    {
      return;
    }
  }

  s_cur->response[s_cur_pos] = response;
  s_cur_pos++;
  s_next_sample++;
  UsbStreamService_NoteSample();

  if (s_cur_pos >= USB_STREAM_FRAME_RESPONSES)
  {
    intan_stream_finalize_frame();
  }
}

uint32_t IntanStream_PeekNextSample(void)
{
  return s_next_sample;
}

uint8_t IntanStream_IsActive(void)
{
  return s_active;
}
