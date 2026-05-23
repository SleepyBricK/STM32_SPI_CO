#include "usb_stream_ring.h"
#include <stddef.h>

static UsbStreamFrame s_frames[USB_FRAME_COUNT]
    __attribute__((section(".dma_buffer"), aligned(32)));
static volatile UsbFrameState s_state[USB_FRAME_COUNT];

typedef struct {
  UsbStreamFrame *q[USB_READY_FIFO_DEPTH];
  uint16_t head;
  uint16_t tail;
  uint16_t count;
} ReadyFrameFifo;

static ReadyFrameFifo s_ready_fifo;

static uint32_t frame_index(const UsbStreamFrame *frame)
{
  ptrdiff_t off = frame - s_frames;

  if (off < 0 || off >= (ptrdiff_t)USB_FRAME_COUNT)
  {
    return USB_FRAME_COUNT;
  }

  return (uint32_t)off;
}

static uint8_t ready_push(ReadyFrameFifo *f, UsbStreamFrame *frame)
{
  if (f->count >= USB_READY_FIFO_DEPTH)
  {
    return 0U;
  }

  f->q[f->tail] = frame;
  f->tail = (uint16_t)((f->tail + 1U) % USB_READY_FIFO_DEPTH);
  f->count++;
  return 1U;
}

static uint8_t ready_push_front(ReadyFrameFifo *f, UsbStreamFrame *frame)
{
  if (f->count >= USB_READY_FIFO_DEPTH)
  {
    return 0U;
  }

  f->head = (uint16_t)((f->head + USB_READY_FIFO_DEPTH - 1U) % USB_READY_FIFO_DEPTH);
  f->q[f->head] = frame;
  f->count++;
  return 1U;
}

static uint8_t ready_pop(ReadyFrameFifo *f, UsbStreamFrame **out)
{
  if (f->count == 0U || out == NULL)
  {
    return 0U;
  }

  *out = f->q[f->head];
  f->head = (uint16_t)((f->head + 1U) % USB_READY_FIFO_DEPTH);
  f->count--;
  return 1U;
}

void UsbStreamRing_Reset(void)
{
  uint32_t i;

  s_ready_fifo.head = 0U;
  s_ready_fifo.tail = 0U;
  s_ready_fifo.count = 0U;

  for (i = 0U; i < USB_FRAME_COUNT; i++)
  {
    s_state[i] = FRAME_FREE;
  }
}

UsbStreamFrame *UsbStreamRing_AcquireFilling(void)
{
  uint32_t i;

  for (i = 0U; i < USB_FRAME_COUNT; i++)
  {
    if (s_state[i] == FRAME_FREE)
    {
      s_state[i] = FRAME_FILLING;
      return &s_frames[i];
    }
  }

  return NULL;
}

uint8_t UsbStreamRing_MarkReady(UsbStreamFrame *frame)
{
  uint32_t idx = frame_index(frame);

  if (idx >= USB_FRAME_COUNT)
  {
    return 0U;
  }

  if (ready_push(&s_ready_fifo, frame) == 0U)
  {
    return 0U;
  }

  s_state[idx] = FRAME_READY;
  return 1U;
}

UsbStreamFrame *UsbStreamRing_PopReady(void)
{
  UsbStreamFrame *frame = NULL;

  if (ready_pop(&s_ready_fifo, &frame) == 0U)
  {
    return NULL;
  }

  return frame;
}

uint8_t UsbStreamRing_PushReadyFront(UsbStreamFrame *frame)
{
  uint32_t idx = frame_index(frame);

  if (idx >= USB_FRAME_COUNT)
  {
    return 0U;
  }

  if (ready_push_front(&s_ready_fifo, frame) == 0U)
  {
    return 0U;
  }

  s_state[idx] = FRAME_READY;
  return 1U;
}

void UsbStreamRing_MarkTxBusy(UsbStreamFrame *frame)
{
  uint32_t idx = frame_index(frame);

  if (idx < USB_FRAME_COUNT)
  {
    s_state[idx] = FRAME_TX_BUSY;
  }
}

void UsbStreamRing_MarkFree(UsbStreamFrame *frame)
{
  uint32_t idx = frame_index(frame);

  if (idx < USB_FRAME_COUNT)
  {
    s_state[idx] = FRAME_FREE;
  }
}

uint32_t UsbStreamRing_ReadyCount(void)
{
  return (uint32_t)s_ready_fifo.count;
}

uint32_t UsbStreamRing_FreeCount(void)
{
  uint32_t i;
  uint32_t n = 0U;

  for (i = 0U; i < USB_FRAME_COUNT; i++)
  {
    if (s_state[i] == FRAME_FREE)
    {
      n++;
    }
  }

  return n;
}
