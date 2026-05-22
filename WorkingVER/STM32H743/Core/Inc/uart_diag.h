#ifndef UART_DIAG_H
#define UART_DIAG_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

void UART_DiagMark(const char *line);
void UART_DiagPrintf(const char *fmt, ...);

void UART_DiagBanner(void);
void UART_DiagDumpResetCause(void);
void UART_DiagDumpClocks(void);
void UART_DiagDumpPwrUsb(void);
void UART_DiagDumpRccUsbPll3(void);
void UART_DiagDumpOtgHsRegs(void);
void UART_DiagDumpPcd(const char *tag);
void UART_DiagDumpUsbd(const char *tag);

#ifdef __cplusplus
}
#endif

#endif /* UART_DIAG_H */
