#ifndef USART_H
#define USART_H

#include "main.h"

extern UART_HandleTypeDef huart1;

void MX_USART1_UART_Init(void);
void UART_Log(const char *line);

#endif
