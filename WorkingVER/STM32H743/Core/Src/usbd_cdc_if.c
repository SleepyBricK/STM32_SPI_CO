#include "usbd_cdc_if.h"
#include "usb_device.h"
#include <string.h>

#define APP_RX_DATA_SIZE  2048U
#define APP_TX_DATA_SIZE  2048U

static uint8_t UserRxBuffer[APP_RX_DATA_SIZE];
static uint8_t UserTxBuffer[APP_TX_DATA_SIZE];

static USBD_CDC_LineCodingTypeDef LineCoding =
{
  115200,
  0x00,
  0x00,
  0x08
};

static int8_t CDC_Init(void);
static int8_t CDC_DeInit(void);
static int8_t CDC_Control(uint8_t cmd, uint8_t *pbuf, uint16_t length);
static int8_t CDC_Receive(uint8_t *pbuf, uint32_t *Len);
static int8_t CDC_TransmitCplt(uint8_t *pbuf, uint32_t *Len, uint8_t epnum);

USBD_CDC_ItfTypeDef USBD_CDC_fops =
{
  CDC_Init,
  CDC_DeInit,
  CDC_Control,
  CDC_Receive,
  CDC_TransmitCplt
};

static int8_t CDC_Init(void)
{
  USBD_CDC_SetRxBuffer(&hUsbDeviceHS, UserRxBuffer);
  return USBD_OK;
}

static int8_t CDC_DeInit(void)
{
  return USBD_OK;
}

static int8_t CDC_Control(uint8_t cmd, uint8_t *pbuf, uint16_t length)
{
  (void)length;

  switch (cmd)
  {
    case CDC_SET_LINE_CODING:
      LineCoding.bitrate = (uint32_t)(pbuf[0] | (pbuf[1] << 8) |
                                      (pbuf[2] << 16) | (pbuf[3] << 24));
      LineCoding.format = pbuf[4];
      LineCoding.paritytype = pbuf[5];
      LineCoding.datatype = pbuf[6];
      break;

    case CDC_GET_LINE_CODING:
      pbuf[0] = (uint8_t)(LineCoding.bitrate);
      pbuf[1] = (uint8_t)(LineCoding.bitrate >> 8);
      pbuf[2] = (uint8_t)(LineCoding.bitrate >> 16);
      pbuf[3] = (uint8_t)(LineCoding.bitrate >> 24);
      pbuf[4] = LineCoding.format;
      pbuf[5] = LineCoding.paritytype;
      pbuf[6] = LineCoding.datatype;
      break;

    default:
      break;
  }

  return USBD_OK;
}

static int8_t CDC_Receive(uint8_t *pbuf, uint32_t *Len)
{
  if (*Len > APP_TX_DATA_SIZE)
  {
    *Len = APP_TX_DATA_SIZE;
  }

  (void)memcpy(UserTxBuffer, pbuf, *Len);
  USBD_CDC_SetTxBuffer(&hUsbDeviceHS, UserTxBuffer, (uint16_t)(*Len));
  USBD_CDC_TransmitPacket(&hUsbDeviceHS);

  USBD_CDC_SetRxBuffer(&hUsbDeviceHS, UserRxBuffer);
  USBD_CDC_ReceivePacket(&hUsbDeviceHS);
  return USBD_OK;
}

static int8_t CDC_TransmitCplt(uint8_t *pbuf, uint32_t *Len, uint8_t epnum)
{
  (void)pbuf;
  (void)Len;
  (void)epnum;
  return USBD_OK;
}
