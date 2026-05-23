#ifndef INTAN_STREAM_H
#define INTAN_STREAM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void IntanStream_Reset(void);
void IntanStream_Begin(void);
void IntanStream_End(void);
void IntanStream_PushResponse(uint16_t response);
uint32_t IntanStream_PeekNextSample(void);
uint8_t IntanStream_IsActive(void);

#ifdef __cplusplus
}
#endif

#endif /* INTAN_STREAM_H */
