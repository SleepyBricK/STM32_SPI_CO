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

#ifndef INC_RHDREGISTERS_H_
#define INC_RHDREGISTERS_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum ZcheckCs {
	ZcheckCs100fF,
	ZcheckCs1pF,
	ZcheckCs10pF
} ZcheckCs;


typedef enum ZcheckPolarity {
	ZcheckPositiveInput,
	ZcheckNegativeInput
} ZcheckPolarity;


typedef enum RHDCommandType {
	RHDCommandConvert,
	RHDCommandCalibrate,
	RHDCommandCalClear,
	RHDCommandRegWrite,
	RHDCommandRegRead
} RHDCommandType;


typedef struct rhdconfigparameters {
	double sample_rate;
	uint8_t adc_reference_bw;
	bool amp_fast_settle;
	bool amp_vref_enable;
	uint8_t adc_comparator_bias;
	uint8_t adc_comparator_select;
	bool vdd_sense_enable;
	uint8_t adc_buffer_bias;
	uint8_t mux_bias;
	uint8_t mux_load;
	bool temp_S2;
	bool temp_S1;
	bool temp_en;
	bool digOutHiZ;
	bool digOut;
	bool weak_miso;
	bool twos_comp;
	bool abs_mode;
	bool DSP_en;
	uint8_t DSP_cutoff_freq;
	bool zcheck_DAC_power;
	bool zcheck_load;
	uint8_t zcheck_scale;
	bool zcheck_conn_all;
	bool zcheck_sel_pol;
	bool zcheck_en;
	uint8_t zcheck_select;
	bool off_chip_RH1;
	bool off_chip_RH2;
	bool off_chip_RL;
	bool adc_Aux1_en;
	bool adc_Aux2_en;
	bool adc_Aux3_en;
	uint8_t rH1_DAC1;
	uint8_t rH1_DAC2;
	uint8_t rH2_DAC1;
	uint8_t rH2_DAC2;
	uint8_t rL_DAC1;
	uint8_t rL_DAC2;
	uint8_t rL_DAC3;
	bool amp_pwr[32];
} RHDConfigParameters;


#define MAX_NUM_CHANNELS_PER_CHIP 32

void set_zcheck_scale(RHDConfigParameters* const p, ZcheckCs scale);
void set_zcheck_polarity(RHDConfigParameters* const p, ZcheckPolarity polarity);
int set_zcheck_channel(RHDConfigParameters* const p, int channel);

void set_default_rhd_settings(RHDConfigParameters* const p);

uint16_t get_register_value(const RHDConfigParameters* const p, int reg);

#define CALIBRATE 			  	(0x5500)
#define CLEAR     				(0x6a00)

#define CONVERT_MASK (0b0)
#define WRITE_MASK   (0b10 << 14)
#define READ_MASK 	 (0b11 << 14)

#define H_BIT (0b1 << 0)


// Run analog-to-digital conversion on specified channel.
// If h_flag is true when DSP offset removal is enabled, then the output of the digital HPF is reset to zero.
// A special case with channel = 63 can be used to cycle through successive amplifier channels,
// so long as at least one defined-channel convert command is called first.
// Once sent, SPI returns (2 commands later) the 16-bit result of this conversion.
// Command: 00_C[5]-C[0]_0000000H for channel C and h_flag H
// Result: A[15]-A[0] for ADC conversion output A
static inline uint16_t convert_command(uint8_t channel, bool h_flag)
{
	const uint16_t convert_channel_mask = CONVERT_MASK | (channel << 8);
	return (h_flag ? H_BIT : 0) | convert_channel_mask;
}


// Initiate ADC self-calibration routine.
// Self-calibration should be performed after chip power-up and register configuration.
// This takes 9 clock cycles to execute - 9 "dummy" commands should be sent after a calibrate command.
// These dummy commands are not executed (unless another calibration command is sent, which resets the process).
// During the entire 9-command process, the results are all 0s except the for the MSB.
// The MSB will be 0 if 2's complement mode is enabled (see Register 4), otherwise it will be 1.
// Command: 01010101_00000000
// Result:  *0000000_00000000 where * depends on 2's complement mode
static inline uint16_t calibrate_command(void)
{
	return CALIBRATE;
}


// Clear ADC calibration.
// Clears the calibration parameters acquired by running the above calibrate command.
// In normal operation, it is not necessary to execute this command.
// Once sent, SPI returns (2 commands later) all 0s except for the MSB.
// The MSB will be 0 if 2's complement is enabled (see Register 4), otherwise it will be 1.
// Command: 01101010_00000000
// Result:  *0000000_00000000 where * depends on 2's complement mode
static inline uint16_t clear_command(void)
{
	return CLEAR;
}


// Write data to register.
// Writes 8 bits of data to specified registers.
// Once sent, SPI returns (2 commands later) 8 MSBs of 1s, and 8 LSBs of the
// echoed data that was written (to verify reception of correct data).
// Any attempt to write to a read-only register (or non-existent register) will produce this same result,
// but data will not be written to that register.
// Command: 10_R[5]-R[0]_D[7]-D[0]
// Result:  11111111_D[7]-D[0]
static inline uint16_t write_command(uint8_t reg_addr, uint8_t data)
{
	return WRITE_MASK | (reg_addr << 8) | data;
}


// Read contents of register.
// Once sent, SPI returns (2 commands later) 8 MSBs of 0s, and 8 LSBs of the read data.
// Command: 11_R[5]-R[0]_00000000
// Result:  00000000_D[7]-D[0]
static inline uint32_t read_command(uint8_t reg_addr)
{
	return READ_MASK | (reg_addr << 8);
}

#endif /* INC_RHDREGISTERS_H_ */
