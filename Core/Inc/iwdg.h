#ifndef __IWDG_H__
#define __IWDG_H__

#include "main.h"

extern IWDG_HandleTypeDef hiwdg1;

void MX_IWDG_Init(void);
/** Latch and clear the RCC IWDG reset cause before normal operation. */
void Iwdg_CaptureResetCause(void);
uint8_t Iwdg_WasReset(void);
/** Refresh at most once per 250 ms, only when the passed main path is healthy. */
void Iwdg_RefreshIfHealthy(uint8_t healthy);

#endif /* __IWDG_H__ */
