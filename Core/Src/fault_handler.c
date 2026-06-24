#include "fault_handler.h"
#include "main.h"
#include "usart.h"

#define FAULT_RECORD_MAGIC  0x464C5433UL

typedef struct {
  uint32_t magic;
  uint32_t fault_id;
  uint32_t cfsr;
  uint32_t hfsr;
  uint32_t mmfar;
  uint32_t bfar;
  uint32_t r0;
  uint32_t r1;
  uint32_t r2;
  uint32_t r3;
  uint32_t r12;
  uint32_t lr;
  uint32_t pc;
  uint32_t psr;
} FaultRecord;

static volatile FaultRecord s_fault_record __attribute__((section(".noinit"), used, aligned(4)));

uint8_t FaultHandler_GetLastFault(void)
{
  if (s_fault_record.magic != FAULT_RECORD_MAGIC || s_fault_record.fault_id > FAULT_NMI)
  {
    return FAULT_NONE;
  }
  return (uint8_t)s_fault_record.fault_id;
}

void fault_handler_enter(FaultHandlerId id, const uint32_t *stacked)
{
  uint8_t groups = 1U;
  uint8_t blinks = 3U;

  __disable_irq();
  s_fault_record.magic = FAULT_RECORD_MAGIC;
  s_fault_record.fault_id = (uint32_t)id;
  s_fault_record.cfsr = SCB->CFSR;
  s_fault_record.hfsr = SCB->HFSR;
  s_fault_record.mmfar = SCB->MMFAR;
  s_fault_record.bfar = SCB->BFAR;
  if (stacked != NULL)
  {
    s_fault_record.r0 = stacked[0];
    s_fault_record.r1 = stacked[1];
    s_fault_record.r2 = stacked[2];
    s_fault_record.r3 = stacked[3];
    s_fault_record.r12 = stacked[4];
    s_fault_record.lr = stacked[5];
    s_fault_record.pc = stacked[6];
    s_fault_record.psr = stacked[7];
  }

  switch (id)
  {
    case FAULT_HARD: groups = 3U; blinks = 3U; break;
    case FAULT_MEMMANAGE: blinks = 2U; break;
    case FAULT_BUS: blinks = 4U; break;
    case FAULT_USAGE: blinks = 5U; break;
    case FAULT_NMI: blinks = 6U; break;
    default: break;
  }

  UART_EarlyMinInit(64000000U);
  UART_SosBlinkPattern(groups, blinks);
}
