#ifndef INTAN_STREAM_H
#define INTAN_STREAM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void IntanStream_Reset(void);
void IntanStream_Begin(void);
void IntanStream_BeginWithMeta(uint16_t frame_flags, uint32_t stream_meta);
void IntanStream_End(void);
void IntanStream_PushResponse(uint16_t response);
void IntanStream_PushCounterBlock(uint32_t base, uint32_t count);
void IntanStream_PushBlock(const uint16_t *src, uint32_t count);
void IntanStream_PushBlockTaggedFromAdc(const uint16_t *adc, uint32_t count, uint8_t first_channel,
                                        uint8_t channel_count, uint8_t phase);
uint32_t IntanStream_PeekNextSample(void);
uint8_t IntanStream_IsActive(void);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_STREAM_H */
