/**
 * @file intan_app.h
 * @brief Высокоуровневые сценарии Intan RHS2116 (инициализация, стимуляция, бенч CONVERT).
 *        Согласовано с msu-neuro-terminal-linux (stimulate_channel0.py, intan_udp_recorder.py).
 */

#ifndef INTAN_APP_H
#define INTAN_APP_H

#include "main.h"
#include "intan_spi.h"

#ifdef __cplusplus
extern "C" {
#endif

void Intan_App_DWT_Reset(void);

HAL_StatusTypeDef Intan_App_InitStim(void);
HAL_StatusTypeDef Intan_App_InitRecord(uint16_t adc_ksps);

HAL_StatusTypeDef Intan_App_ClearAdc(void);
HAL_StatusTypeDef Intan_App_ClearCompliance(void);

/** Бит i = канал i (0…15). */
HAL_StatusTypeDef Intan_App_StimSetupCurrents(uint16_t ch_mask, unsigned neg_ua, unsigned pos_ua);
HAL_StatusTypeDef Intan_App_StimEnable(uint16_t ch_mask, uint8_t enable, uint8_t negative_polarity);
HAL_StatusTypeDef Intan_App_SetStimMagnitude(uint8_t channel, unsigned magnitude_ua, uint8_t is_positive);

/**
 * Пилообразная стимуляция (блокирующая). Полярность — положительная, как --sawtooth в Python.
 * @param period_ms период одного цикла пилы (сумма задержек между шагами)
 */
HAL_StatusTypeDef Intan_App_StimSawtooth(uint16_t ch_mask, unsigned steps, unsigned max_ua,
                                         uint32_t period_ms, uint32_t cycles);

/**
 * n вызовов CONVERT для оценки скорости. Время — DWT CYCCNT, SystemCoreClock.
 * @param channel 0…15 или 63 (автообход каналов RHS2116)
 * @param out_ksps_total тысяч CONVERT/с
 * @param out_ksps_per_ch при channel==63 — тысяч полных «отсчётов на канал»/с (total/16); иначе = total
 */
HAL_StatusTypeDef Intan_App_BenchConvert(uint32_t n, uint8_t channel, float *out_ksps_total,
                                         float *out_ksps_per_ch);
HAL_StatusTypeDef Intan_App_BenchConvertFast(uint32_t n, uint8_t channel, float *out_ksps_total,
                                             float *out_ksps_per_ch);
HAL_StatusTypeDef Intan_App_BenchConvertTimCs(uint32_t n, uint8_t channel, uint32_t target_ksps,
                                              float *out_ksps_total, float *out_ksps_per_ch);
HAL_StatusTypeDef Intan_App_BenchConvertDmaTimCs(uint32_t n, uint8_t channel, float *out_ksps_total,
                                                 float *out_ksps_per_ch);

/**
 * Разбор спецификации каналов: ALL / * / 0,2,4 / 0-3 / смесь.
 * @return 0 при успехе, -1 при ошибке
 */
int Intan_App_ParseChMask(const char *spec, uint16_t *out_mask);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_APP_H */
