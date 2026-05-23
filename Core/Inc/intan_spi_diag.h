#ifndef INTAN_SPI_DIAG_H
#define INTAN_SPI_DIAG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint32_t spi_kernel_hz;
  uint32_t spi_prescaler_div;
  uint32_t spi_sck_hz_calc;
  uint32_t tim1_hz;
  uint32_t tim_period_ticks;
  uint32_t sample_period_avg_cycles;
  uint32_t block_cycles_last;
  uint32_t block_samples_last;
  uint32_t block_slots_last;
  /** DWT wall-clock over full acquisition (includes setup/recover), per sample. */
  uint32_t wall_cyc_per_sample;
  uint32_t wall_cycles_total;
  uint32_t wall_samples_last;
} IntanSpiDiagSnapshot;

void Intan_SpiDiag_Init(void);
void Intan_SpiDiag_ReadClockConfig(IntanSpiDiagSnapshot *out);
void Intan_SpiDiag_ResetTiming(void);
void Intan_SpiDiag_RecordBlock(uint32_t cyc_start, uint32_t cyc_end, uint32_t samples,
                               uint32_t slots, uint32_t tim_period_ticks);
void Intan_SpiDiag_RecordWall(uint32_t cyc_start, uint32_t cyc_end, uint32_t samples);
uint32_t Intan_SpiDiag_KspsFromCycX10(uint32_t cyc_per_sample);
const IntanSpiDiagSnapshot *Intan_SpiDiag_GetSnapshot(void);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_SPI_DIAG_H */
