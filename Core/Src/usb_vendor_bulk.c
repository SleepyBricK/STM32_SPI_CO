#include "usb_vendor_bulk.h"
#include "usbd_ctlreq.h"
#include "usbd_conf.h"
#include "stm32h7xx_ll_usb.h"

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
  0x01, 0x01, USBD_IDX_CONFIG_STR, 0xC0, USBD_MAX_POWER,
  0x09, USB_DESC_TYPE_INTERFACE,
  VENDOR_BULK_INTERFACE, 0x00, 0x02, 0xFF, 0x00, 0x00, USBD_IDX_INTERFACE_STR,
  0x07, USB_DESC_TYPE_ENDPOINT,
  VENDOR_BULK_OUT_EP, USBD_EP_TYPE_BULK,
  LOBYTE(VENDOR_BULK_MAX_PACKET), HIBYTE(VENDOR_BULK_MAX_PACKET), 0x00,
  0x07, USB_DESC_TYPE_ENDPOINT,
  VENDOR_BULK_IN_EP, USBD_EP_TYPE_BULK,
  LOBYTE(VENDOR_BULK_MAX_PACKET), HIBYTE(VENDOR_BULK_MAX_PACKET), 0x00,
};

__ALIGN_BEGIN static uint8_t USBD_VENDOR_BULK_DeviceQualifierDesc[USB_LEN_DEV_QUALIFIER_DESC] __ALIGN_END =
{
  USB_LEN_DEV_QUALIFIER_DESC, USB_DESC_TYPE_DEVICE_QUALIFIER,
  0x00, 0x02, 0x00, 0x00, 0x00, USB_MAX_EP0_SIZE, 0x01, 0x00,
};

__ALIGN_BEGIN static uint8_t s_rx[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;
__ALIGN_BEGIN static uint8_t s_pending_rx[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;
__ALIGN_BEGIN static uint8_t s_txt_stash[VENDOR_BULK_HS_MAX_PACKET] __ALIGN_END;

static USBD_HandleTypeDef *s_pdev;
static volatile uint8_t s_txt_busy;
static volatile uint8_t s_frame_active;
static volatile uint32_t s_frame_len;
static volatile uint8_t s_rx_pending;
static volatile uint16_t s_rx_len;
static uint8_t s_alt;
static USBD_VENDOR_BULK_TxCompleteFn s_tx_cb;

static uint8_t vendor_in_xfer_done(void)
{
  PCD_HandleTypeDef *hpcd;
  PCD_EPTypeDef *inep;

  if ((s_pdev == NULL) || (s_frame_active == 0U))
  {
    return 0U;
  }

  hpcd = (PCD_HandleTypeDef *)s_pdev->pData;
  inep = &hpcd->IN_ep[VENDOR_BULK_IN_EP & EP_ADDR_MSK];
  return (inep->xfer_count >= inep->xfer_len) ? 1U : 0U;
}

static uint8_t USBD_VENDOR_BULK_Init(USBD_HandleTypeDef *pdev, uint8_t cfgidx)
{
  (void)cfgidx;

  s_pdev = pdev;
  s_txt_busy = 0U;
  s_frame_active = 0U;
  s_frame_len = 0U;
  s_rx_pending = 0U;
  s_rx_len = 0U;

  USBD_LL_OpenEP(pdev, VENDOR_BULK_IN_EP, USBD_EP_TYPE_BULK, VENDOR_BULK_MAX_PACKET);
  USBD_LL_OpenEP(pdev, VENDOR_BULK_OUT_EP, USBD_EP_TYPE_BULK, VENDOR_BULK_MAX_PACKET);
  USBD_LL_PrepareReceive(pdev, VENDOR_BULK_OUT_EP, s_rx, VENDOR_BULK_MAX_PACKET);
  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_DeInit(USBD_HandleTypeDef *pdev, uint8_t cfgidx)
{
  (void)cfgidx;

  USBD_LL_CloseEP(pdev, VENDOR_BULK_IN_EP);
  USBD_LL_CloseEP(pdev, VENDOR_BULK_OUT_EP);
  s_pdev = NULL;
  s_txt_busy = 0U;
  s_frame_active = 0U;
  s_rx_pending = 0U;
  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_Setup(USBD_HandleTypeDef *pdev, USBD_SetupReqTypedef *req)
{
  if ((req->bmRequest & USB_REQ_TYPE_MASK) == USB_REQ_TYPE_STANDARD)
  {
    switch (req->bRequest)
    {
      case USB_REQ_GET_INTERFACE:
        s_alt = 0U;
        USBD_CtlSendData(pdev, &s_alt, 1U);
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
  USBD_VENDOR_BULK_TxCompleteFn cb;
  uint32_t done;

  (void)pdev;
  if ((epnum & 0x7FU) != (VENDOR_BULK_IN_EP & 0x7FU))
  {
    return (uint8_t)USBD_OK;
  }

  if (s_frame_active != 0U)
  {
    if (vendor_in_xfer_done() == 0U)
    {
      return (uint8_t)USBD_OK;
    }

    done = s_frame_len;
    s_frame_active = 0U;
    s_frame_len = 0U;
    cb = s_tx_cb;
    if (cb != NULL)
    {
      cb(done);
    }
    return (uint8_t)USBD_OK;
  }

  s_txt_busy = 0U;
  return (uint8_t)USBD_OK;
}

static uint8_t USBD_VENDOR_BULK_DataOut(USBD_HandleTypeDef *pdev, uint8_t epnum)
{
  uint32_t n;

  if ((epnum & 0x7FU) == (VENDOR_BULK_OUT_EP & 0x7FU))
  {
    n = USBD_LL_GetRxDataSize(pdev, epnum);
    if ((n > 0U) && (n <= VENDOR_BULK_MAX_PACKET) && (s_rx_pending == 0U))
    {
      USBD_memcpy(s_pending_rx, s_rx, n);
      s_rx_len = (uint16_t)n;
      s_rx_pending = 1U;
    }
    USBD_LL_PrepareReceive(pdev, VENDOR_BULK_OUT_EP, s_rx, VENDOR_BULK_MAX_PACKET);
  }

  return (uint8_t)USBD_OK;
}

uint8_t USBD_VENDOR_BULK_Transmit(uint8_t *buf, uint16_t len)
{
  if ((s_pdev == NULL) || (s_pdev->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > VENDOR_BULK_MAX_PACKET) ||
      (s_txt_busy != 0U) || (s_frame_active != 0U))
  {
    return (uint8_t)USBD_FAIL;
  }

  USBD_memcpy(s_txt_stash, buf, len);
  s_txt_busy = 1U;
  if (USBD_LL_Transmit(s_pdev, VENDOR_BULK_IN_EP, s_txt_stash, len) != USBD_OK)
  {
    s_txt_busy = 0U;
    return (uint8_t)USBD_FAIL;
  }

  return (uint8_t)USBD_OK;
}

uint8_t USBD_VENDOR_BULK_TransmitFrame(const uint8_t *buf, uint32_t len)
{
  if ((s_pdev == NULL) || (s_pdev->dev_state != USBD_STATE_CONFIGURED) ||
      (buf == NULL) || (len == 0U) || (len > USB_STREAM_FRAME_SIZE) ||
      (s_frame_active != 0U) || (s_txt_busy != 0U))
  {
    return (uint8_t)USBD_FAIL;
  }

  s_frame_active = 1U;
  s_frame_len = len;
  if (USBD_LL_Transmit(s_pdev, VENDOR_BULK_IN_EP, (uint8_t *)buf, len) != USBD_OK)
  {
    s_frame_active = 0U;
    s_frame_len = 0U;
    return (uint8_t)USBD_FAIL;
  }

  return (uint8_t)USBD_OK;
}

void USBD_VENDOR_BULK_AbortFrame(void)
{
  PCD_HandleTypeDef *hpcd;
  uint32_t primask = __get_PRIMASK();

  __disable_irq();
  if ((s_pdev != NULL) && (s_frame_active != 0U))
  {
    uint32_t USBx_BASE;

    hpcd = (PCD_HandleTypeDef *)s_pdev->pData;
    if (hpcd != NULL)
    {
      (void)HAL_PCD_EP_Abort(hpcd, VENDOR_BULK_IN_EP);
      (void)HAL_PCD_EP_Flush(hpcd, VENDOR_BULK_IN_EP);

      /* Discard completion state from the transfer which was just aborted. */
      USBx_BASE = (uint32_t)hpcd->Instance;
      USBx_INEP(VENDOR_BULK_IN_EP & EP_ADDR_MSK)->DIEPINT =
          USB_OTG_DIEPINT_XFRC | USB_OTG_DIEPINT_EPDISD;
    }
    s_frame_active = 0U;
    s_frame_len = 0U;
  }
  if (primask == 0U)
  {
    __enable_irq();
  }
}

void USBD_VENDOR_BULK_SetTxCompleteCallback(USBD_VENDOR_BULK_TxCompleteFn cb)
{
  s_tx_cb = cb;
}

uint8_t USBD_VENDOR_BULK_TxIdle(void)
{
  return (s_pdev != NULL) &&
         (s_pdev->dev_state == USBD_STATE_CONFIGURED) &&
         (s_frame_active == 0U) &&
         (s_txt_busy == 0U);
}

uint8_t USBD_VENDOR_BULK_PollRx(uint8_t *buf, uint16_t max_len, uint16_t *len_out)
{
  uint16_t n;

  if ((buf == NULL) || (len_out == NULL) || (s_rx_pending == 0U))
  {
    return 0U;
  }

  __disable_irq();
  if (s_rx_pending == 0U)
  {
    __enable_irq();
    return 0U;
  }

  n = s_rx_len;
  if (n > max_len)
  {
    n = max_len;
  }
  USBD_memcpy(buf, s_pending_rx, n);
  s_rx_pending = 0U;
  s_rx_len = 0U;
  __enable_irq();

  *len_out = n;
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
