#include "usbd_vendor_bulk.h"
#include "usbd_ctlreq.h"
#include "stm32h7xx.h"

#define VENDOR_BULK_CONFIG_DESC_SIZE     32U
#define VENDOR_BULK_INTERFACE            0U

#ifndef USB_PCD_DMA_ENABLE
#define USB_PCD_DMA_ENABLE               1U
#endif

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
} VendorBulkTxItem;

static __ALIGN_BEGIN VendorBulkTxItem VendorBulkTxQ[VENDOR_BULK_TX_QUEUE_DEPTH] __ALIGN_END;
static volatile uint8_t VendorBulkTxQHead;
static volatile uint8_t VendorBulkTxQTail;
static volatile uint8_t VendorBulkTxQCount;
static volatile uint8_t VendorBulkTxHwBusy;
static volatile uint8_t VendorBulkRxPending;
static volatile uint16_t VendorBulkPendingRxLen;
static USBD_HandleTypeDef *VendorBulkDevice;
static uint8_t VendorBulkAltSetting;

static void vendor_bulk_dcache_clean(const uint8_t *ptr, uint32_t len)
{
#if (USB_PCD_DMA_ENABLE == 1U)
  if ((len > 0U) && (ptr != NULL))
  {
    uint32_t start = (uint32_t)ptr & ~31U;
    uint32_t end = ((uint32_t)ptr + len + 31U) & ~31U;
    SCB_CleanDCache_by_Addr((uint32_t *)start, end - start);
  }
#else
  (void)ptr;
  (void)len;
#endif
}

static uint8_t vendor_bulk_tx_launch(const VendorBulkTxItem *item)
{
  const uint8_t *tx_ptr = (item->zc != 0U) ? item->ptr : item->stash;

  vendor_bulk_dcache_clean(tx_ptr, item->len);
  VendorBulkTxHwBusy = 1U;
  if (USBD_LL_Transmit(VendorBulkDevice, VENDOR_BULK_IN_EP, (uint8_t *)tx_ptr, item->len) != USBD_OK)
  {
    VendorBulkTxHwBusy = 0U;
    return (uint8_t)USBD_FAIL;
  }

  return (uint8_t)USBD_OK;
}

static void vendor_bulk_tx_kick(void)
{
  if ((VendorBulkTxHwBusy != 0U) || (VendorBulkTxQCount == 0U))
  {
    return;
  }

  (void)vendor_bulk_tx_launch(&VendorBulkTxQ[VendorBulkTxQHead]);
}

static void vendor_bulk_tx_complete(void)
{
  VendorBulkTxHwBusy = 0U;
  if (VendorBulkTxQCount > 0U)
  {
    VendorBulkTxQHead = (uint8_t)((VendorBulkTxQHead + 1U) % VENDOR_BULK_TX_QUEUE_DEPTH);
    VendorBulkTxQCount--;
  }
  vendor_bulk_tx_kick();
}

static uint8_t vendor_bulk_tx_enqueue(VendorBulkTxItem *item)
{
  if (VendorBulkTxQCount >= VENDOR_BULK_TX_QUEUE_DEPTH)
  {
    return (uint8_t)USBD_BUSY;
  }

  VendorBulkTxQ[VendorBulkTxQTail] = *item;
  VendorBulkTxQTail = (uint8_t)((VendorBulkTxQTail + 1U) % VENDOR_BULK_TX_QUEUE_DEPTH);
  VendorBulkTxQCount++;
  vendor_bulk_tx_kick();
  return (uint8_t)USBD_OK;
}

static void vendor_bulk_tx_reset(void)
{
  VendorBulkTxQHead = 0U;
  VendorBulkTxQTail = 0U;
  VendorBulkTxQCount = 0U;
  VendorBulkTxHwBusy = 0U;
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
    vendor_bulk_tx_complete();
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
  VendorBulkTxItem item;

  if ((VendorBulkDevice == NULL) || (VendorBulkDevice->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > VENDOR_BULK_MAX_PACKET))
  {
    return (uint8_t)USBD_FAIL;
  }

  USBD_memcpy(item.stash, buf, len);
  item.ptr = item.stash;
  item.len = len;
  item.zc = 0U;
  return vendor_bulk_tx_enqueue(&item);
}

uint8_t USBD_VENDOR_BULK_TransmitZc(const uint8_t *buf, uint16_t len)
{
  VendorBulkTxItem item;

  if ((VendorBulkDevice == NULL) || (VendorBulkDevice->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > VENDOR_BULK_MAX_PACKET))
  {
    return (uint8_t)USBD_FAIL;
  }

  item.ptr = buf;
  item.len = len;
  item.zc = 1U;
  return vendor_bulk_tx_enqueue(&item);
}

uint8_t USBD_VENDOR_BULK_TxReady(void)
{
  return (VendorBulkDevice != NULL) &&
         (VendorBulkDevice->dev_state == USBD_STATE_CONFIGURED) &&
         (VendorBulkTxQCount < VENDOR_BULK_TX_QUEUE_DEPTH);
}

uint8_t USBD_VENDOR_BULK_TxIdle(void)
{
  return (VendorBulkDevice != NULL) &&
         (VendorBulkDevice->dev_state == USBD_STATE_CONFIGURED) &&
         (VendorBulkTxQCount == 0U) &&
         (VendorBulkTxHwBusy == 0U);
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
