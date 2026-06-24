#ifndef FAULT_HANDLER_H
#define FAULT_HANDLER_H

#include <stdint.h>

typedef enum {
  FAULT_NONE = 0,
  FAULT_HARD = 1,
  FAULT_MEMMANAGE = 2,
  FAULT_BUS = 3,
  FAULT_USAGE = 4,
  FAULT_NMI = 5,
} FaultHandlerId;

void fault_handler_enter(FaultHandlerId id, const uint32_t *stacked);
uint8_t FaultHandler_GetLastFault(void);

#endif /* FAULT_HANDLER_H */
