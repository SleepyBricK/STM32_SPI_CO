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

#ifndef INC_USERCONFIG_H_
#define INC_USERCONFIG_H_

// If using HAL drivers, leave this uncommented.
// If using LL drivers, leave this commented.
#define USE_HAL

// If acquiring a short period of data, then exiting and transmitting data
// offline is desired, leave this uncommented.
// If instead transmitting data in real-time is desired, leave this commented.
#define OFFLINE_TRANSFER

// Error detect GPIO, by default used to illuminate red LED
// when an error of any kind is detected.
#define ERROR_DETECTED_PORT 		LED_RED_GPIO_Port
#define ERROR_DETECTED_PIN 			LED_RED_Pin
// If this pin goes high (red LED illuminates), check error code
// bits 0-3 to determine which error code has been flagged.
// Default GPIO assignment:
// PE0: ErrorCode_Bit_3 (MSB)
// PG8: ErrorCode_Bit_2
// PG5: ErrorCode_Bit_1
// PG6: ErrorCode_Bit_0 (LSB)
// Once the 4-bit error code has been determined, consult ErrorCode in
// rhdinterface.h to determine to which error the code maps.

// How many CONVERT commands are sent in a single sequence,
// which occurs every time the period defined by INTERRUPT_TIM occurs (default 20 kHz).
// Default of 32 indicates that 32 amplifier channels are each sampled once per sequence.
#define CONVERT_COMMANDS_PER_SEQUENCE 32
#define AUX_OFFSET CONVERT_COMMANDS_PER_SEQUENCE

// How many AUX commands are sent in a single sequence,
// which occurs every time the period defined by INTERRUPT_TIM occurs (default 20 kHz).
// Default of 3 indicates that 3 auxiliary command lists
// each execute a single command per sequence.
#define AUX_COMMANDS_PER_SEQUENCE 3

// How many AUX commands are contained in a single auxiliary
// command list (excluding zcheck_DAC command lists).
// Default of 128 indicates that for each sequence, each of the
// AUX_COMMANDS_PER_SEQUENCE (default 3) command lists executes a single command,
// so that 128 sequences must occur before an auxiliary command
// list finishes and repeats execution from the beginning again.
#define AUX_COMMAND_LIST_LENGTH 128

// IMPORTANT note regarding sample rate:
// The per-channel sample rate is defined by the period of the INTERRUPT_TIM peripheral,
// which generates the interrupt events that trigger SPI sequence transfers.
// The INTERRUPT_TIM peripheral configuration occurs in the .ioc file, and by default
// is set up with the timer's input frequency of 275 MHz (APB1 Timer clock) and a period
// of 13750 cycles, resulting in 20 kHz.

// When recording offline, how many seconds of data should be acquired before
// the acquisition loop is escaped. With the default sample rate of 20000, 1 second of data
// corresponds to 20000 samples per channel.
// Note that on-chip RAM is limited, so setting this number excessively high will cause
// the chip to run out of memory during program execution.
#define NUMBER_OF_SECONDS_TO_ACQUIRE 0.5

// Which of the RHD chip's amplifier channels is selected as the starting point to have its data
// saved and transmitted via USART.
#define FIRST_SAMPLED_CHANNEL 8

// How many channels (starting from FIRST_SAMPLED_CHANNEL) to have their data saved and transmitted via USART.
#define NUM_SAMPLED_CHANNELS 4

// Which peripherals/handles should be used by software, depending on if HAL or LL drivers are used.
// If the user wishes to use a different peripheral, for example SPI2 instead of SPI3, then that
// change should be made here (in addition to configuring that peripheral properly in the .ioc file).
#ifdef USE_HAL
#define USART huart3
#define SPI hspi3
#define INTERRUPT_TIM htim3
#else
#define USART USART3
#define SPI SPI3
#define INTERRUPT_TIM TIM3
#define DMA DMA1
#define DMA_TX_CHANNEL LL_DMA_STREAM_1
#define DMA_RX_CHANNEL LL_DMA_STREAM_0
#define DMA_USART_CHANNEL LL_DMA_STREAM_2
#endif

#endif /* INC_USERCONFIG_H_ */
