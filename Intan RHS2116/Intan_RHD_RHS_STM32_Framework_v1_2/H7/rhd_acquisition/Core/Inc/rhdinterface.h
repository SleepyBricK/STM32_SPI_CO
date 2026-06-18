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

#ifndef INC_RHDINTERFACE_H_
#define INC_RHDINTERFACE_H_

#include "userconfig.h"
#include "rhdregisters.h"
#include "main.h"
#include <stdlib.h>


// Error Conditions.
typedef enum
{
	NoError = 0, // No Error
	TxDMAError = 1, // Transmit DMA Error - specific error condition outlined in STM32H7 reference manual.
	RxDMAError = 2, // Receive DMA Error - specific error condition outlined in STM32H7 reference manual.
	TxSPIError = 3, // Transmit SPI Error - specific error condition outlined in STM32H7 reference manual.
	RxSPIError = 4, // Receive SPI Error - specific error condition outlined in STM32H7 reference manual.
	SampleClip = 5, // TIM-triggered interrupt occurs before the previous sample processing routine finished execution,
					// indicating that TIM period is too short, likely fixed by reducing sample rate by increasing TIM
					// period or reducing number of commands per sequence.
	TxUSARTError = 6, // Transmit USART Error - some issue with the transmission of data across USART.
	OutOfMemoryError = 7 // Attempted dynamic allocation of memory failed - not enough available memory.
} ErrorCode;


typedef enum {
	TRANSFER_WAIT,
	TRANSFER_COMPLETE,
	TRANSFER_ERROR
} TransferState;


extern volatile uint16_t command_sequence_MOSI[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE];
extern volatile uint16_t command_sequence_MISO[CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE];
extern volatile uint16_t next_aux_commands[AUX_COMMANDS_PER_SEQUENCE];

extern uint16_t sample_counter;
extern uint16_t *sample_memory;
extern uint32_t per_channel_sample_memory_capacity;

extern uint16_t aux_command_list[AUX_COMMANDS_PER_SEQUENCE][AUX_COMMAND_LIST_LENGTH];

// Unused, unless configuring zcheck DAC in configure_aux_commands(), in userfunctions.c
//static int8_t zcheck_DAC_command_slot_position = -1;
//static int16_t zcheck_DAC_command_list_length = -1;

extern volatile bool main_loop_active;
extern volatile bool uart_ready;
extern volatile bool sample_interrupt_occurred;
extern volatile bool main_pin_status;

extern RHDConfigParameters parameters;

void handle_error(ErrorCode error_code);
void sample_processing_routine(void);

void initialize_spi_with_dma(void);
void end_spi_with_dma(void);

void write_initial_reg_values(RHDConfigParameters* const p);
double calculate_sample_rate(void);
void create_convert_sequence(const uint8_t* const channel_numbers_to_convert);

int create_command_list_RHD_register_config(const RHDConfigParameters* const p, uint16_t* const command_list, bool calibrate, int num_commands);
int create_command_list_RHD_sample_aux_ins(uint16_t* const command_list, int num_commands);
int create_command_list_RHD_update_DigOut(const RHDConfigParameters* const p, uint16_t* const command_list, int num_commands);
int create_command_list_dummy(const RHDConfigParameters* const p, uint16_t* const command_list, int n, uint16_t cmd);
int create_command_list_zcheck_DAC(const RHDConfigParameters* const p, uint16_t* const command_list, double frequency, double amplitude);
void send_spi_command(uint16_t tx_data);

void copy_next_aux_commands_to_MOSI(void);

#ifdef USE_HAL
extern UART_HandleTypeDef USART;
extern SPI_HandleTypeDef SPI;
extern TIM_HandleTypeDef INTERRUPT_TIM;
#else
void begin_spi_rx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words);
void begin_spi_tx(uint32_t mem_increment, uint32_t mem_address, uint32_t num_words);
void end_spi_rx(void);
void end_spi_tx(void);
void dma_interrupt_routine_rx(void);
void dma_interrupt_routine_tx(void);
void dma_interrupt_routine_usart_tx(void);
void spi_interrupt_routine(void);
void uart_interrupt_routine(void);
#endif


// Write specified pin on specified port either high (1) or low (0).
static inline void write_pin(GPIO_TypeDef* const gpio_port, uint32_t gpio_pin, bool level)
{
#ifdef USE_HAL
	HAL_GPIO_WritePin(gpio_port, gpio_pin, level);
#else
	level ? LL_GPIO_SetOutputPin(gpio_port, gpio_pin) : LL_GPIO_ResetOutputPin(gpio_port, gpio_pin);
#endif
}


// Load WRITE command to specified aux slot position in MOSI command sequence list.
static inline void load_write_to_MOSI(uint8_t aux_slot, uint8_t reg_address)
{
	command_sequence_MOSI[AUX_OFFSET + aux_slot] = write_command(reg_address, get_register_value(&parameters, reg_address));
}


// Load READ command to specified aux slot position in MOSI command sequence list.
static inline void load_read_to_MOSI(uint8_t aux_slot, uint8_t reg_address)
{
	command_sequence_MOSI[AUX_OFFSET + aux_slot] = read_command(reg_address);
}


// Calculate suitable size for sample_memory array and allocate memory.
// Note, free_sample_memory() should be called after this function and when memory allocation is no longer needed.
static inline void allocate_sample_memory(void)
{
	per_channel_sample_memory_capacity = calculate_sample_rate() * NUMBER_OF_SECONDS_TO_ACQUIRE;
	uint32_t total_sample_memory_capacity = NUM_SAMPLED_CHANNELS * per_channel_sample_memory_capacity;
	sample_memory = (uint16_t *)malloc(total_sample_memory_capacity * sizeof(uint16_t));
	if (sample_memory == NULL) {
		handle_error(OutOfMemoryError);
	}
}


// Free memory previously allocated for sample_memory array.
// Note, this should be called after allocate_sample_memory() and when memory allocation is no longer needed.
static inline void free_sample_memory(void)
{
	free(sample_memory);
}


#endif /* INC_RHDINTERFACE_H_ */
