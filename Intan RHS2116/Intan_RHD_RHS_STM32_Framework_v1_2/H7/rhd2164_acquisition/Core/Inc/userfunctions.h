/*
  Intan Technologies RHD STM32 Firmware Framework
  Version 1.2

  Copyright (c) 2025 Intan Technologies

  This file is part of the Intan Technologies RHD STM32 Firmware Framework.

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the “Software”), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in
  all copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
  THE SOFTWARE.


  See <http://www.intantech.com> for documentation and product information.
  
 */

#ifndef INC_USERFUNCTIONS_H_
#define INC_USERFUNCTIONS_H_

#include "rhdinterface.h"
#include "stm32h7xx_it.h"

/* Implementations of these functions include code for both HAL
 * (USE_HAL is defined in userconfig.h) and LL (USE_HAL is not defined in userconfig.h) drivers.
 * Both drivers can generally achieve the same goals, but the more specialized
 * LL drivers tend to be faster than the generalized HAL drivers.
 */

/* START OF DECLARATION OF FUNCTIONS LIKELY TO BE CHANGED BY USER DEPENDING ON SPECIFICIC APPLICATION
 * Users should edit the implementations of these functions to achieve their desired behavior.
 * For example, if the main acquisition while loop should never end, replace loop_escape()'s implementation with 'return 0;'
 * If the the register values to write to the RHD chip should diverge from defaults,
 * set and write the new register values after the initial writes.
 */
// Condition to escape main acquisition while loop. Return 1 to escape, return 0 to stay in while loop.
int loop_escape(void);

// Write data from specific channel(s) to memory for retention across interrupt routine executions.
void write_data_to_memory(void);

// Transmit data (for example via USART) in realtime, executed once every interrupt routine execution.
void transmit_data_realtime(void);

// Transmit data (for example via USART) after acquisition, executed once after escaping the main acquisition while loop.
void transmit_data_offline(void);

// Configure registers with suitable default values (same as RHX software's defaults), and write them to RHD chip via SPI.
void configure_registers(void);

// Populate command_sequence_MOSI with CONVERT commands; each command in command_sequence_MOSI is executed
// once in ascending order per interrupt routine execution.
void configure_convert_commands(void);

// Populate command_sequence_MOSI with AUX commands; each command in command_sequence_MOSI is executed
// once in ascending order per interrupt routine execution.
void configure_aux_commands(void);

// Use DMA to transmit num_bytes of data from memory pointer tx_data directly to USART.
void transmit_dma_to_usart(volatile const uint16_t* const tx_data, uint16_t num_bytes);

/* END OF DECLARATION OF FUNCTIONS LIKELY TO BE CHANGED BY USER */

/* START OF STATIC INLINE FUNCTIONS NOT LIKELY TO BE CHANGED BY USER
 * These static inline functions can give small performance boosts,
 * helpful for repeated function calls within interrupt routine.
 */


// Wait for 'duration' ms. Recommended to never call from within an interrupt function.
static inline void wait_ms(int duration)
{
#ifdef USE_HAL
	HAL_Delay(duration);
#else
	SysTick_Config(SystemCoreClock/1000); // Set up SysTick so that getSysTick() returns ms since program started.
	while (get_SysTick() < duration) {}
#endif
}


// Enable/disable timer interrupts.
static inline void enable_interrupt_timer(bool enable)
{
#ifdef USE_HAL
	enable ? HAL_TIM_Base_Start_IT(&INTERRUPT_TIM) : HAL_TIM_Base_Stop_IT(&INTERRUPT_TIM);
#else
	if (enable) {
		LL_TIM_EnableCounter(INTERRUPT_TIM);
		LL_TIM_EnableIT_UPDATE(INTERRUPT_TIM);
	} else {
		LL_TIM_DisableCounter(INTERRUPT_TIM);
		LL_TIM_DisableIT_UPDATE(INTERRUPT_TIM);
	}
#endif
}

/* END OF STATIC INLINE FUNCTIONS NOT LIKELY TO BE CHANGED BY USER */

#endif /* INC_USERFUNCTIONS_H_ */
