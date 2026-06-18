/*
  Intan Technologies RHS STM32 Firmware Framework
  Version 1.2

  Copyright (c) 2025 Intan Technologies

  This file is part of the Intan Technologies RHS STM32 Firmware Framework.

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

#ifndef INC_RHSREGISTERS_H_
#define INC_RHSREGISTERS_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum DigOut {
	DigOut1,
	DigOut2,
	DigOutOD
} DigOut;

typedef enum ChargeRecoveryCurrentLimit {
	CurrentLimitMin = 0,
    CurrentLimit1nA,
    CurrentLimit2nA,
    CurrentLimit5nA,
    CurrentLimit10nA,
    CurrentLimit20nA,
    CurrentLimit50nA,
    CurrentLimit100nA,
    CurrentLimit200nA,
    CurrentLimit500nA,
    CurrentLimit1uA,
    CurrentLimitMax
} ChargeRecoveryCurrentLimit;

typedef enum StimStepSize {
	StimStepSizeUnrecognized = -1,
    StimStepSizeMin = 0,
    StimStepSize10nA = 1,
    StimStepSize20nA = 2,
    StimStepSize50nA = 3,
    StimStepSize100nA = 4,
    StimStepSize200nA = 5,
    StimStepSize500nA = 6,
    StimStepSize1uA = 7,
    StimStepSize2uA = 8,
    StimStepSize5uA = 9,
    StimStepSize10uA = 10,
    StimStepSizeMax = 11
} StimStepSize;

typedef enum ZcheckCs {
	ZcheckCs100fF,
	ZcheckCs1pF,
	ZcheckCs10pF
} ZcheckCs;


typedef enum RHSCommandType {
	RHSCommandConvert,
	RHSCommandCalClear,
	RHSCommandRegWrite,
	RHSCommandRegRead,
	RHSCommandComplianceReset
} RHSCommandType;


typedef struct rhsconfigparameters {
	double sample_rate;
	uint8_t adc_buffer_bias;
	uint8_t mux_bias;
	uint8_t DSP_cutoff_freq;
	bool DSP_en;
	bool abs_mode;
	bool twos_comp;
	bool weak_miso;
	bool digOut1HiZ;
	bool digOut1;
	bool digOut2HiZ;
	bool digOut2;
	bool digOutOD;
	bool zcheck_en;
	uint8_t zcheck_scale;
	bool zcheck_load;
	bool zcheck_DAC_power;
	uint8_t zcheck_select;
	uint8_t rH1_sel1;
	uint8_t rH1_sel2;
	uint8_t rH2_sel1;
	uint8_t rH2_sel2;
	uint8_t rL_A_sel1;
	uint8_t rL_A_sel2;
	uint8_t rL_A_sel3;
	uint8_t rL_B_sel1;
	uint8_t rL_B_sel2;
	uint8_t rL_B_sel3;
	bool amp_pwr[16];
	bool amp_fast_settle[16];
	bool amp_f_l_select[16];
	uint16_t stim_enableA;
	uint16_t stim_enableB;
	uint8_t stim_step_sel1;
	uint8_t stim_step_sel2;
	uint8_t stim_step_sel3;
	uint8_t stimN_bias;
	uint8_t stimP_bias;
	uint8_t charge_recovery_DAC;
	uint8_t charge_recovery_current_limit_sel1;
	uint8_t charge_recovery_current_limit_sel2;
	uint8_t charge_recovery_current_limit_sel3;
	bool dc_amp_pwr[16];
	bool compliance_monitor[16];
	bool stim_on[16];
	bool stim_pol[16];
	bool charge_recovery_switch[16];
	bool c_l_charge_recovery_en[16];
	uint8_t neg_current_mag[16];
	uint8_t neg_current_trim[16];
	uint8_t pos_current_mag[16];
	uint8_t pos_current_trim[16];
} RHSConfigParameters;


#define MAX_NUM_CHANNELS_PER_CHIP 16

void set_zcheck_scale(RHSConfigParameters* const p, ZcheckCs scale);
int set_zcheck_channel(RHSConfigParameters* const p, int channel);

void set_stim_step_size(RHSConfigParameters* const p, StimStepSize step);
int set_pos_stim_magnitude(RHSConfigParameters* const p, int channel, int magnitude, int trim);
int set_neg_stim_magnitude(RHSConfigParameters* const p, int channel, int magnitude, int trim);

void set_stim_on(RHSConfigParameters* const p, int channel, bool on);
void set_stim_polarity(RHSConfigParameters* const p, int channel, bool positive);
void set_charge_recovery_switch(RHSConfigParameters* const p, int channel, bool connect_to_stim_gnd);
void set_c_l_charge_recovery_enable(RHSConfigParameters* const p, int channel, bool enable);

void set_amp_fast_settle(RHSConfigParameters* const p, int channel, bool settle);
void set_amp_f_l_select(RHSConfigParameters* const p, int channel, uint8_t lower_freq_A);

void set_charge_recovery_current_limit(RHSConfigParameters* const p, ChargeRecoveryCurrentLimit limit);
double set_charge_recovery_target_voltage(RHSConfigParameters* const p, double v_target);

void set_default_rhs_settings(RHSConfigParameters* const p);

uint16_t get_register_value(const RHSConfigParameters*  const p, int reg);

#define CALIBRATE 			  	(0x55000000)
#define CLEAR     				(0x6a000000)
#define COMPLIANCE_RESET_READ   (0xd0ff0000)

#define CONVERT_MASK (0b0)
#define WRITE_MASK   (0b10 << 30)
#define READ_MASK 	 (0b11 << 30)

#define U_BIT (0b1 << 29)
#define M_BIT (0b1 << 28)
#define D_BIT (0b1 << 27)
#define H_BIT (0b1 << 26)


// Run analog-to-digital conversion on specified channel.
// U flag: Setting the U (Update) flag to one updates all "triggered registers" to new values that were previously programmed.
// M flag: Setting the M (Monitor) flag to one clears the compliance monitor register (Register 40).
// D flag: If the D (DC amplifier) flag of a CONVERT command is set to one then the DC low-gain amplifier of channel C is
// 		   also sampled (with 10-bit resolution), and its value is returned in the lower 10 bits of the result.
// H flag: If the H (High-pass filter) flag of a CONVERT command is set to one when DSP offset removal is enabled then the
//		   output of the digital high-pass filter associated with amplifier channel C is reset to zero. This can be used to
//		   rapidly recover from a large transient and settle to baseline.
// A special case with channel = 63 can be used to cycle through successive amplifier channels,
// so long as at least one defined-channel convert command is called first.
// Once sent, SPI returns (2 commands later) the 16-bit result of this conversion.
// Command: 00UMDH0000_C[5]-C[0]_0000000000000000 for channel C
// Result: A[15]-A[0]_000000_W[9:0] for ADC conversion output A, and if D flag is set, for DC low-gain conversion output W
static inline uint32_t convert_command(uint8_t channel, bool u_flag, bool m_flag, bool d_flag, bool h_flag)
{
	const uint32_t convert_channel_mask = CONVERT_MASK | (channel << 16);
	return (u_flag ? U_BIT : 0) | (m_flag ? M_BIT : 0) | (d_flag ? D_BIT : 0) | (h_flag ? H_BIT : 0) | convert_channel_mask;
}


// The CALIBRATE command was included in the Intan Technologies RHD2000 family of amplifier-only chips to initiate an ADC
// self-calibration routine that was performed after chip power-up and register configuration. Although the command is included
// in this chip for continuity, use of the CALIBRATE command is not recommended for the RHS2116. Rather, a CLEAR command
// should be issued after the chip has been powered up (see next command).
static inline uint32_t calibrate_command(void) // OBSOLETE
{
	return CALIBRATE;
}


// Clear ADC calibration.
// The CLEAR command initializes the ADC on the RHS2116 for normal operation. This command should be executed once after
// chip power-up to maximize the precision of the ADC.
// Once sent, SPI returns (2 commands later) all 0s except for the MSB.
// The MSB will be 0 if 2's complement mode is enabled (see Register 4), otherwise it will be 1.
// Command: 01101010_00000000_00000000_00000000
// Result:  *0000000_00000000_00000000_00000000 where * depends on 2's complement mode
static inline uint32_t clear_command(void)
{
	return CLEAR;
}


// Write data to register.
// Writes 16 bits of data to specified registers.
// Once sent, SPI returns (2 commands later) 16 MSBs of 1s, and 16 LSBs of the
// echoed data that was written (to verify reception of correct data).
// Any attempt to write to a read-only register (or non-existent register) will produce this same result,
// but data will not be written to that register.
// Command: 10UM0000_R[7]-R[0]_D[15]-D[0]
// Result:  1111111111111111_D[15]-D[0]
static inline uint32_t write_command(uint8_t reg_addr, uint16_t data, bool u_flag, bool m_flag)
{
	return WRITE_MASK | (u_flag ? U_BIT : 0) | (m_flag ? M_BIT : 0) | (reg_addr << 16) | data;
}


// Read contents of register.
// Once sent, SPI returns (2 commands later) 16 MSBs of 0s, and 16 LSBs of the read data.
// Command: 11UM0000_R[7]-R[0]_0000000000000000
// Result:  0000000000000000_D[15]-D[0]
static inline uint32_t read_command(uint8_t reg_addr, bool u_flag, bool m_flag)
{
	return READ_MASK | ( u_flag ? U_BIT : 0) | (m_flag ? M_BIT : 0) | (reg_addr << 16);
}


// Read from register 255 with M flag set to initiate compliance monitor reset
static inline uint32_t compliance_reset_command(void)
{
	return COMPLIANCE_RESET_READ;
}

#endif /* INC_RHSREGISTERS_H_ */
