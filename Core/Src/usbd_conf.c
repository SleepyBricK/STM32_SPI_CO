#include "usbd_conf.h"
#include "usbd_core.h"
#include "usart.h"
#include "usb3300_ulpi_hw.h"
#include <stdio.h>

extern USBD_HandleTypeDef hUsbDeviceHS;

PCD_HandleTypeDef hpcd_USB_OTG_HS;

volatile uint32_t g_usb_ev_reset;
volatile uint32_t g_usb_ev_connect;
volatile uint32_t g_usb_ev_disconnect;

static USBD_StatusTypeDef USBD_Get_USB_Status(HAL_StatusTypeDef hal_status);

void HAL_PCD_SetupStageCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_SetupStage((USBD_HandleTypeDef *)hpcd->pData, (uint8_t *)hpcd->Setup);
}

void HAL_PCD_DataOutStageCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_DataOutStage((USBD_HandleTypeDef *)hpcd->pData, epnum, hpcd->OUT_ep[epnum].xfer_buff);
}

void HAL_PCD_DataInStageCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_DataInStage((USBD_HandleTypeDef *)hpcd->pData, epnum, hpcd->IN_ep[epnum].xfer_buff);
}

void HAL_PCD_SOFCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_SOF((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ResetCallback(PCD_HandleTypeDef *hpcd)
{
  g_usb_ev_reset++;
  USBD_LL_SetSpeed((USBD_HandleTypeDef *)hpcd->pData,
                   (hpcd->Init.speed == PCD_SPEED_HIGH) ? USBD_SPEED_HIGH : USBD_SPEED_FULL);
  USBD_LL_Reset((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_SuspendCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_Suspend((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ResumeCallback(PCD_HandleTypeDef *hpcd)
{
  USBD_LL_Resume((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_ISOOUTIncompleteCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_IsoOUTIncomplete((USBD_HandleTypeDef *)hpcd->pData, epnum);
}

void HAL_PCD_ISOINIncompleteCallback(PCD_HandleTypeDef *hpcd, uint8_t epnum)
{
  USBD_LL_IsoINIncomplete((USBD_HandleTypeDef *)hpcd->pData, epnum);
}

void HAL_PCD_ConnectCallback(PCD_HandleTypeDef *hpcd)
{
  g_usb_ev_connect++;
  USBD_LL_DevConnected((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_DisconnectCallback(PCD_HandleTypeDef *hpcd)
{
  g_usb_ev_disconnect++;
  USBD_LL_DevDisconnected((USBD_HandleTypeDef *)hpcd->pData);
}

void HAL_PCD_MspInit(PCD_HandleTypeDef *pcdHandle)
{
  if (pcdHandle->Instance == USB_OTG_HS)
  {
    UART_DebugMark("[USB] HAL_PCD_MspInit HS enter\r\n");
    USB3300_ULPI_HwInit();

    HAL_NVIC_SetPriority(OTG_HS_IRQn, 5, 0);
    HAL_NVIC_EnableIRQ(OTG_HS_IRQn);
    UART_DebugMark("[USB] HAL_PCD_MspInit HS done\r\n");
  }
}

void HAL_PCD_MspDeInit(PCD_HandleTypeDef *pcdHandle)
{
  if (pcdHandle->Instance == USB_OTG_HS)
  {
    __HAL_RCC_USB1_OTG_HS_CLK_DISABLE();
    __HAL_RCC_USB1_OTG_HS_ULPI_CLK_DISABLE();
    HAL_NVIC_DisableIRQ(OTG_HS_IRQn);
  }
}

USBD_StatusTypeDef USBD_LL_Init(USBD_HandleTypeDef *pdev)
{
  if (pdev->id == DEVICE_HS)
  {
    hpcd_USB_OTG_HS.pData = pdev;
    pdev->pData = &hpcd_USB_OTG_HS;

    hpcd_USB_OTG_HS.Instance = USB_OTG_HS;
    hpcd_USB_OTG_HS.Init.dev_endpoints = 9;
#if (USB_DEBUG_FORCE_FULL_SPEED == 1U)
    UART_DebugMark("[USB] DEBUG forcing Full Speed over ULPI\r\n");
    hpcd_USB_OTG_HS.Init.speed = PCD_SPEED_HIGH_IN_FULL;
#else
    hpcd_USB_OTG_HS.Init.speed = PCD_SPEED_HIGH;
#endif
    hpcd_USB_OTG_HS.Init.dma_enable = ENABLE;
    hpcd_USB_OTG_HS.Init.phy_itface = PCD_PHY_ULPI;
    hpcd_USB_OTG_HS.Init.Sof_enable = DISABLE;
    hpcd_USB_OTG_HS.Init.low_power_enable = DISABLE;
    hpcd_USB_OTG_HS.Init.lpm_enable = DISABLE;
    hpcd_USB_OTG_HS.Init.battery_charging_enable = DISABLE;
    hpcd_USB_OTG_HS.Init.vbus_sensing_enable = DISABLE;
    hpcd_USB_OTG_HS.Init.use_dedicated_ep1 = DISABLE;

    UART_DebugMark("[USB] HAL_PCD_Init HS...\r\n");
    if (HAL_PCD_Init(&hpcd_USB_OTG_HS) != HAL_OK)
    {
      UART_DebugMark("[USB] HAL_PCD_Init HS failed\r\n");
      Error_Handler();
    }
    UART_DebugMark("[USB] HAL_PCD_Init HS OK\r\n");

    HAL_PCDEx_SetRxFiFo(&hpcd_USB_OTG_HS, 0x180);
    HAL_PCDEx_SetTxFiFo(&hpcd_USB_OTG_HS, 0, 0x40);
    HAL_PCDEx_SetTxFiFo(&hpcd_USB_OTG_HS, 1, 0x180);
  }

  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_DeInit(USBD_HandleTypeDef *pdev)
{
  return USBD_Get_USB_Status(HAL_PCD_DeInit((PCD_HandleTypeDef *)pdev->pData));
}

USBD_StatusTypeDef USBD_LL_Start(USBD_HandleTypeDef *pdev)
{
  return USBD_Get_USB_Status(HAL_PCD_Start((PCD_HandleTypeDef *)pdev->pData));
}

USBD_StatusTypeDef USBD_LL_Stop(USBD_HandleTypeDef *pdev)
{
  return USBD_Get_USB_Status(HAL_PCD_Stop((PCD_HandleTypeDef *)pdev->pData));
}

USBD_StatusTypeDef USBD_LL_OpenEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                  uint8_t ep_type, uint16_t ep_mps)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_Open((PCD_HandleTypeDef *)pdev->pData,
                                             ep_addr, ep_mps, ep_type));
}

USBD_StatusTypeDef USBD_LL_CloseEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_Close((PCD_HandleTypeDef *)pdev->pData, ep_addr));
}

USBD_StatusTypeDef USBD_LL_FlushEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_Flush((PCD_HandleTypeDef *)pdev->pData, ep_addr));
}

USBD_StatusTypeDef USBD_LL_StallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_SetStall((PCD_HandleTypeDef *)pdev->pData, ep_addr));
}

USBD_StatusTypeDef USBD_LL_ClearStallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_ClrStall((PCD_HandleTypeDef *)pdev->pData, ep_addr));
}

uint8_t USBD_LL_IsStallEP(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  PCD_HandleTypeDef *hpcd = (PCD_HandleTypeDef *)pdev->pData;

  if ((ep_addr & 0x80U) == 0x80U)
  {
    return hpcd->IN_ep[ep_addr & 0x7FU].is_stall;
  }

  return hpcd->OUT_ep[ep_addr & 0x7FU].is_stall;
}

USBD_StatusTypeDef USBD_LL_SetUSBAddress(USBD_HandleTypeDef *pdev, uint8_t dev_addr)
{
  return USBD_Get_USB_Status(HAL_PCD_SetAddress((PCD_HandleTypeDef *)pdev->pData, dev_addr));
}

USBD_StatusTypeDef USBD_LL_Transmit(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                    uint8_t *pbuf, uint32_t size)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_Transmit((PCD_HandleTypeDef *)pdev->pData,
                                                 ep_addr, pbuf, size));
}

USBD_StatusTypeDef USBD_LL_PrepareReceive(USBD_HandleTypeDef *pdev, uint8_t ep_addr,
                                          uint8_t *pbuf, uint32_t size)
{
  return USBD_Get_USB_Status(HAL_PCD_EP_Receive((PCD_HandleTypeDef *)pdev->pData,
                                                ep_addr, pbuf, size));
}

uint32_t USBD_LL_GetRxDataSize(USBD_HandleTypeDef *pdev, uint8_t ep_addr)
{
  return HAL_PCD_EP_GetRxCount((PCD_HandleTypeDef *)pdev->pData, ep_addr);
}

void USBD_LL_Delay(uint32_t Delay)
{
  HAL_Delay(Delay);
}

void *USBD_static_malloc(uint32_t size)
{
  static uint32_t mem[(sizeof(USBD_HandleTypeDef) + 3U) / 4U];
  (void)size;
  return mem;
}

void USBD_static_free(void *p)
{
  (void)p;
}

static USBD_StatusTypeDef USBD_Get_USB_Status(HAL_StatusTypeDef hal_status)
{
  switch (hal_status)
  {
    case HAL_OK:
      return USBD_OK;
    case HAL_BUSY:
      return USBD_BUSY;
    default:
      return USBD_FAIL;
  }
}

void USBD_LogStatus(const char *tag)
{
  char line[160];
  USB_OTG_GlobalTypeDef *usb = USB_OTG_HS;
  USB_OTG_DeviceTypeDef *dev = (USB_OTG_DeviceTypeDef *)((uint32_t)usb + USB_OTG_DEVICE_BASE);
  uint32_t dsts = dev->DSTS;
  uint32_t dctl = dev->DCTL;
  uint32_t gotg = usb->GOTGCTL;

  (void)snprintf(line, sizeof(line),
                 "[USB] %s dev_state=%u rst=%lu conn=%lu disc=%lu "
                 "DSTS=0x%08lX DCTL=0x%08lX GOTGCTL=0x%08lX\r\n",
                 (tag != NULL) ? tag : "?",
                 (unsigned)hUsbDeviceHS.dev_state,
                 (unsigned long)g_usb_ev_reset,
                 (unsigned long)g_usb_ev_connect,
                 (unsigned long)g_usb_ev_disconnect,
                 (unsigned long)dsts,
                 (unsigned long)dctl,
                 (unsigned long)gotg);
  UART_DebugMark(line);
}
