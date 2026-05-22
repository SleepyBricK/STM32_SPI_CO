#ifndef USB3300_ULPI_HW_H
#define USB3300_ULPI_HW_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32h7xx_hal.h"

/* См. USB3300-Hardware-Design-Checklist-00002886A.pdf в корне проекта. */
void USB3300_ULPI_HwInit(void);

#ifdef __cplusplus
}
#endif

#endif /* USB3300_ULPI_HW_H */
