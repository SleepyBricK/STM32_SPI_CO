#ifndef INTAN_PATTERN_H
#define INTAN_PATTERN_H

#include "main.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define INTAN_PATTERN_MAX_SLOTS 1024U

typedef enum {
  INTAN_PATTERN_SLOT_SPI = 0,
  INTAN_PATTERN_SLOT_DELAY_CYCLES = 1,
  INTAN_PATTERN_SLOT_DELAY_US = 2
} IntanPatternSlotKind;

typedef struct {
  uint8_t kind;
  uint8_t _pad0;
  uint16_t _pad1;
  uint32_t arg;
} IntanPatternSlot;

typedef struct {
  uint32_t slot_count;
  uint32_t spi_slots;
  uint32_t delay_slots;
  uint8_t loaded;
  uint8_t running;
  uint8_t last_error;
  uint8_t _pad;
} IntanPatternStatus;

void Intan_Pattern_Clear(void);
HAL_StatusTypeDef Intan_Pattern_AddRawWord(uint32_t word);
HAL_StatusTypeDef Intan_Pattern_AddWrite(uint8_t reg_addr, uint16_t value, uint8_t u_flag, uint8_t m_flag);
HAL_StatusTypeDef Intan_Pattern_AddRead(uint8_t reg_addr);
HAL_StatusTypeDef Intan_Pattern_AddConvert(uint8_t channel, uint8_t flags);
HAL_StatusTypeDef Intan_Pattern_AddClearAdc(void);
HAL_StatusTypeDef Intan_Pattern_AddClearCompliance(void);
HAL_StatusTypeDef Intan_Pattern_AddDelayCycles(uint32_t cycles);
HAL_StatusTypeDef Intan_Pattern_AddDelayUs(uint32_t us);
HAL_StatusTypeDef Intan_Pattern_Run(uint32_t repeat_count);
void Intan_Pattern_GetStatus(IntanPatternStatus *status);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_PATTERN_H */
