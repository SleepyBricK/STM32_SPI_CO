#include "usbd_vendor_bulk.h"
#include "usbd_ctlreq.h"

#define VENDOR_BULK_CONFIG_DESC_SIZE     32U
#define VENDOR_BULK_INTERFACE            0U

static uint8_t USBD_VENDOR_BULK_Init(USBD_HandleTypeDef *pdev, uint8_t cfgidx);
static uint8_t USBD_VENDOR_BULK_DeInit(USBD_HandleTypeDef *pdev, uint8_t cfgidx);
static uint8_t USBD_VENDOR_BULK_Setup(USBD_HandleTypeDef *pdev, USBD_SetupReqTypedef *req);
static uint8_t USBD_VENDOR_BULK_DataIn(USBD_HandleTypeDef *pdev, uint8_t epnum);
static uint8_t USBD_VENDOR_BULK_DataOut(USBD_HandleTypeDef *pdev, uint8_t epnum);
static uint8_t *USBD_VENDOR_BULK_GetCfgDesc(uint16_t *length);
static uint8_t *USBD_VENDOR_BULK_GetDeviceQualifierDesc(uint16_t *length);

USBD_ClassTypeDef USBD_VENDOR_BULK =
{
  USBD_VENDOR_BULK_Init,
  USBD_VENDOR_BULK_DeInit,
  USBD_VENDOR_BULK_Setup,
  NULL,
  NULL,
  USBD_VENDOR_BULK_DataIn,
  USBD_VENDOR_BULK_DataOut,
  NULL,
  NULL,
  NULL,
  USBD_VENDOR_BULK_GetCfgDesc,
  USBD_VENDOR_BULK_GetCfgDesc,
  USBD_VENDOR_BULK_GetCfgDesc,
  USBD_VENDOR_BULK_GetDeviceQualifierDesc,
};

__ALIGN_BEGIN static uint8_t USBD_VENDOR_BULK_CfgDesc[VENDOR_BULK_CONFIG_DESC_SIZE] __ALIGN_END =
{
  0x09, USB_DESC_TYPE_CONFIGURATION,
  LOBYTE(VENDOR_BULK_CONFIG_DESC_SIZE), HIBYTE(VENDOR_BULK_CONFIG_DESC_SIZE),
  0x01,
  0x01,
  USBD_IDX_CONFIG_STR,
  0xC0,
  USBD_MAX_POWER,

  0x09, USB_DESC_TYPE_INTERFACE,
  VENDOR_BULK_INTERFACE,
  0x00,
  0x02,
  0xFF,
  0x00,
  0x00,
  USBD_IDX_INTERFACE_STR,

  0x07, USB_DESC_TYPE_ENDPOINT,
  VENDOR_BULK_OUT_EP,
  USBD_EP_TYPE_BULK,
  LOBYTE(VENDOR_BULK_MAX_PACKET), HIBYTE(VENDOR_BULK_MAX_PACKET),
  0x00,

  0x07, USB_DESC_TYPE_ENDPOINT,
  VENDOR_BULK_IN_EP,
  USBD_EP_TYPE_BULK,
  LOBYTE(VENDOR_BULK_MAX_PACKET), HIBYTE(VENDOR_BULK_MAX_PACKET),
  0x00,
};

__ALIGN_BEGIN static uint8_t USBD_VENDOR_BULK_DeviceQualifierDesc[USB_LEN_DEV_QUALIFIER_DESC] __ALIGN_END =
{
  USB_LEN_DEV_QUALIFIER_DESC,
  USB_DESC_TYPE_DEVICE_QUALIFIER,
  0x00, 0x02,
  0x00,
  0x00,
  0x00,
  USB_MAX_EP0_SIZE,
  0x01,
  0x00,
};

__ALIGN_BEGIN static uint8_t VendorBulkRxBuffer[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;
__ALIGN_BEGIN static uint8_t VendorBulkPendingRx[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;

typedef struct
{
  const uint8_t *ptr;
  uint16_t len;
  uint8_t zc;
  __ALIGN_BEGIN uint8_t stash[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;
} VendorBulkTxPacket;

static __ALIGN_BEGIN VendorBulkTxPacket VendorBulkTxActive __ALIGN_END;
static __ALIGN_BEGIN VendorBulkTxPacket VendorBulkTxQueued __ALIGN_END;

static volatile uint8_t VendorBulkTxInFlight;
static volatile uint8_t VendorBulkTxHasQueued;
static volatile uint8_t VendorBulkRxPending;
static volatile uint16_t VendorBulkPendingRxLen;
static USBD_HandleTypeDef *VendorBulkDevice;
static uint8_t VendorBulkAltSetting;

static uint8_t vendor_bulk_tx_start_packet(const VendorBulkTxPacket *pkt)
{
  const uint8_t *tx_ptr = (pkt->zc != 0U) ? pkt->ptr : pkt->stash;

  VendorBulkTxInFlight = 1U;
  if (USBD_LL_Transmit(VendorBulkDevice, VENDOR_BULK_IN_EP, (uint8_t *)tx_ptr, pkt->len) != USBD_OK)
  {
    VendorBulkTxInFlight = 0U;
    return (uint8_t)USBD_FAIL;
  }

  return (uint8_t)USBD_OK;
}

static void vendor_bulk_tx_store_packet(VendorBulkTxPacket *dst, const uint8_t *buf, uint16_t len, uint8_t zc)
{
  dst->len = len;
  dst->zc = zc;
  if (zc != 0U)
  {
    dst->ptr = buf;
    return;
  }

  USBD_memcpy(dst->stash, buf, len);
  dst->ptr = dst->stash;
}

static void vendor_bulk_tx_kick_queued(void)
{
  if ((VendorBulkTxHasQueued != 0U) && (VendorBulkTxInFlight == 0U))
  {
    VendorBulkTxActive = VendorBulkTxQueued;
    VendorBulkTxHasQueued = 0U;
    (void)vendor_bulk_tx_start_packet(&VendorBulkTxActive);
  }
}

static void vendor_bulk_tx_reset(void)
{
  VendorBulkTxInFlight = 0U;
  VendorBulkTxHasQueued = 0U;
  VendorBulkTxActive.len = 0U;
  VendorBulkTxActive.zc = 0U;
  VendorBulkTxQueued.len = 0U;
  VendorBulkTxQueued.zc = 0U;
}

static uint8_t vendor_bulk_tx_submit(const uint8_t *buf, uint16_t len, uint8_t zc)
{
  if (VendorBulkTxInFlight == 0U)
  {
    vendor_bulk_tx_store_packet(&VendorBulkTxActive, buf, len, zc);
    return vendor_bulk_tx_start_packet(&VendorBulkTxActive);
  }

  if (VendorBulkTxHasQueued == 0U)
  {
    vendor_bulk_tx_store_packet(&VendorBulkTxQueued, buf, len, zc);
    VendorBulkTxHasQueued = 1U;
    return (uint8_t)USBD_OK;
  }

  return (uint8_t)USBD_BUSY;
}

static uint8_t USBD_VENDOR_BULK_Init(USBD_HandleTypeDef *pdev, uint8_t cfgidx)
{
  (void)cfgidx;

  VendorBulkDevice = pdev;
  vendor_bulk_tx_reset();
  VendorBulkRxPending = 0U;
  VendorBulkPendingRxLen = 0U;

  USBD_LL_OpenEP(pdev, VENDOR_BULK_IN_EP, USBD_EP_TYPE_BULK, VENDOR_BULK_MAX_PACKET);
  USBD_LL_OpenEP(pdev, VENDOR_BULK_OUT_EP, USBD_EP_TYPE_BULK, VENDOR_BULK_MAX_PACKET);
  USBD_LL_PrepareReceive(pdev, VENDOR_BULK_OUT_EP, VendorBulkRxBuffer, VENDOR_BULK_MAX_PACKET);

  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_DeInit(USBD_HandleTypeDef *pdev, uint8_t cfgidx)
{
  (void)cfgidx;

  USBD_LL_CloseEP(pdev, VENDOR_BULK_IN_EP);
  USBD_LL_CloseEP(pdev, VENDOR_BULK_OUT_EP);
  VendorBulkDevice = NULL;
  vendor_bulk_tx_reset();
  VendorBulkRxPending = 0U;
  VendorBulkPendingRxLen = 0U;

  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_Setup(USBD_HandleTypeDef *pdev, USBD_SetupReqTypedef *req)
{
  if ((req->bmRequest & USB_REQ_TYPE_MASK) == USB_REQ_TYPE_STANDARD)
  {
    switch (req->bRequest)
    {
      case USB_REQ_GET_INTERFACE:
        VendorBulkAltSetting = 0U;
        USBD_CtlSendData(pdev, &VendorBulkAltSetting, 1U);
        return (uint8_t)USBD_OK;

      case USB_REQ_SET_INTERFACE:
        return (uint8_t)USBD_OK;

      default:
        break;
    }
  }

  USBD_CtlError(pdev, req);
  return (uint8_t)USBD_FAIL;
}

static uint8_t USBD_VENDOR_BULK_DataIn(USBD_HandleTypeDef *pdev, uint8_t epnum)
{
  (void)pdev;
  if ((epnum & 0x7FU) == (VENDOR_BULK_IN_EP & 0x7FU))
  {
    VendorBulkTxInFlight = 0U;
    vendor_bulk_tx_kick_queued();
  }
  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_DataOut(USBD_HandleTypeDef *pdev, uint8_t epnum)
{
  uint32_t rx_len;

  if ((epnum & 0x7FU) == (VENDOR_BULK_OUT_EP & 0x7FU))
  {
    rx_len = USBD_LL_GetRxDataSize(pdev, epnum);

    if ((rx_len > 0U) && (rx_len <= VENDOR_BULK_MAX_PACKET) && (VendorBulkRxPending == 0U))
    {
      USBD_memcpy(VendorBulkPendingRx, VendorBulkRxBuffer, rx_len);
      VendorBulkPendingRxLen = (uint16_t)rx_len;
      VendorBulkRxPending = 1U;
    }

    USBD_LL_PrepareReceive(pdev, VENDOR_BULK_OUT_EP, VendorBulkRxBuffer, VENDOR_BULK_MAX_PACKET);
  }

  return (uint8_t)USBD_OK;
}

uint8_t USBD_VENDOR_BULK_Transmit(uint8_t *buf, uint16_t len)
{
  if ((VendorBulkDevice == NULL) || (VendorBulkDevice->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > VENDOR_BULK_MAX_PACKET))
  {
    return (uint8_t)USBD_FAIL;
  }

  return vendor_bulk_tx_submit(buf, len, 0U);
}

uint8_t USBD_VENDOR_BULK_TransmitZc(const uint8_t *buf, uint16_t len)
{
  if ((VendorBulkDevice == NULL) || (VendorBulkDevice->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > VENDOR_BULK_MAX_PACKET))
  {
    return (uint8_t)USBD_FAIL;
  }

  return vendor_bulk_tx_submit(buf, len, 1U);
}

uint8_t USBD_VENDOR_BULK_TxReady(void)
{
  return (VendorBulkDevice != NULL) &&
         (VendorBulkDevice->dev_state == USBD_STATE_CONFIGURED) &&
         ((VendorBulkTxInFlight == 0U) || (VendorBulkTxHasQueued == 0U));
}

uint8_t USBD_VENDOR_BULK_TxIdle(void)
{
  return (VendorBulkDevice != NULL) &&
         (VendorBulkDevice->dev_state == USBD_STATE_CONFIGURED) &&
         (VendorBulkTxInFlight == 0U) &&
         (VendorBulkTxHasQueued == 0U);
}

uint8_t USBD_VENDOR_BULK_PollRx(uint8_t *buf, uint16_t max_len, uint16_t *len_out)
{
  uint16_t len;

  if ((buf == NULL) || (len_out == NULL) || (VendorBulkRxPending == 0U))
  {
    return 0U;
  }

  __disable_irq();
  if (VendorBulkRxPending == 0U)
  {
    __enable_irq();
    return 0U;
  }

  len = VendorBulkPendingRxLen;
  if (len > max_len)
  {
    len = max_len;
  }
  USBD_memcpy(buf, VendorBulkPendingRx, len);
  VendorBulkRxPending = 0U;
  VendorBulkPendingRxLen = 0U;
  __enable_irq();

  *len_out = len;
  return 1U;
}

static uint8_t *USBD_VENDOR_BULK_GetCfgDesc(uint16_t *length)
{
  *length = sizeof(USBD_VENDOR_BULK_CfgDesc);
  return USBD_VENDOR_BULK_CfgDesc;
}

static uint8_t *USBD_VENDOR_BULK_GetDeviceQualifierDesc(uint16_t *length)
{
  *length = sizeof(USBD_VENDOR_BULK_DeviceQualifierDesc);
  return USBD_VENDOR_BULK_DeviceQualifierDesc;
}
