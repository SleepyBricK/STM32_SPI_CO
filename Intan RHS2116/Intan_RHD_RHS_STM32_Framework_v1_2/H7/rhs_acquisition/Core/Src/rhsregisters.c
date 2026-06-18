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

#include "rhsregisters.h"
#include "userconfig.h"
#include <math.h>


// Convert a size-16 array of bits (each bit allocated as bool) to a 16-bit word.
static inline uint16_t array_to_word(const bool* const a)
{
	uint16_t word = 0;

	for (int i = 0; i < 16; i++) {
		if (a[i]) word += (1 << i);
	}

	return word;
}


// Determine if a provided channel number is out of the valid range.
static inline bool channel_out_of_range(int channel)
{
	return (channel < 0 || channel >= MAX_NUM_CHANNELS_PER_CHIP);
}


// Drive auxiliary output low.
static inline void set_DigOut_low(RHSConfigParameters* const p, DigOut pin)
{
	switch (pin) {
	case DigOut1:
		p->digOut1 = false;
		p->digOut1HiZ = false;
		break;
	case DigOut2:
		p->digOut2 = false;
		p->digOut2HiZ = false;
		break;
	case DigOutOD:
		p->digOutOD = true;
		break;
	}
}


// Drive auxiliary output high.
static inline void set_DigOut_high(RHSConfigParameters* const p, DigOut pin)
{
	switch (pin) {
	case DigOut1:
		p->digOut1 = true;
		p->digOut1HiZ = false;
		break;
	case DigOut2:
		p->digOut2 = true;
		p->digOut2HiZ = false;
		break;
	case DigOutOD:
		p->digOutOD = false;
		break;
	}
}


// Set auxiliary digital output to high-impedance (HiZ) state.
static inline void set_DigOut_hiZ(RHSConfigParameters* const p, DigOut pin)
{
	switch (pin) {
	case DigOut1:
		p->digOut1 = false;
		p->digOut1HiZ = true;
		break;
	case DigOut2:
		p->digOut2 = false;
		p->digOut2HiZ = true;
		break;
	case DigOutOD:
		p->digOutOD = false;
		break;
	}
}


// Set the DSP offset removal filter cutoff frequency as closely to the requested
// newDspCutoffFreq (in Hz) as possible; returns the actual cutoff frequency (in Hz).
static inline double set_DSP_cutoff_freq(RHSConfigParameters* const p, double new_DSP_cutoff_freq)
{
	double f_cutoff[16] = {0};
	// Note: f_cutoff[0] = 0.0 here, but this index should not be used.
	for (int n = 1; n < 16; ++n) {
		double x = pow(2.0, (double) n);
		f_cutoff[n] = p->sample_rate * log(x / (x - 1.0)) / (2.0 * M_PI);
	}
	double log_new_DSP_cutoff_freq = log10(new_DSP_cutoff_freq);

	// Find the closest value to the requested cutoff frequency (on a logarithmic scale).
	if (new_DSP_cutoff_freq > f_cutoff[1]) {
		p->DSP_cutoff_freq = 1;
	} else if (new_DSP_cutoff_freq < f_cutoff[15]) {
		p->DSP_cutoff_freq = 15;
	} else {
		double min_log_diff = 10000000.0;
		for (int n = 1; n < 16; n++) {
			double log_f_cutoff = log10(f_cutoff[n]);
			if (fabs(log_new_DSP_cutoff_freq - log_f_cutoff) < min_log_diff) {
				min_log_diff = fabs(log_new_DSP_cutoff_freq - log_f_cutoff);
				p->DSP_cutoff_freq = n;
			}
		}
	}
	return f_cutoff[p->DSP_cutoff_freq];
}


// Return the current value of the DSP offset removal cutoff frequency (in Hz).
static inline double get_DSP_cutoff_freq(RHSConfigParameters* const p)
{
	double x = pow(2.0, (double) p->DSP_cutoff_freq);

	return p->sample_rate * log(x / (x - 1.0)) / (2.0 * M_PI);
}


// Power up or down selected amplifier on chip.
static inline void set_amp_powered(RHSConfigParameters* const p, int channel, bool powered)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->amp_pwr[channel] = powered;
}


// Power up all amplifiers on chip.
static inline void power_up_all_amps(RHSConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->amp_pwr[channel] = true;
	}
}


// Power down all amplifiers on chip.
static inline void power_down_all_amps(RHSConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->amp_pwr[channel] = false;
	}
}


// Power up or down selected DC amplifier on chip.
static inline void set_DC_amp_powered(RHSConfigParameters* const p, const int channel, bool powered)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->dc_amp_pwr[channel] = powered;
}


// Power up all DC amplifiers on chip
static inline void power_up_all_DC_amps(RHSConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->dc_amp_pwr[channel] = true;
	}
}


// Power down all DC amplifiers on chip
static inline void power_down_all_DC_amps(RHSConfigParameters* const p)
{
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; ++channel) {
		p->dc_amp_pwr[channel] = false;
	}
}


// Enable or disable stimulation globally.
static inline void set_stim_enable(RHSConfigParameters* const p, bool enable)
{
	if (enable) {
		p->stim_enableA = 0xaaaa;
		p->stim_enableB = 0x00ff;
	} else {
		p->stim_enableA = 0x0000;
		p->stim_enableB = 0x0000;
	}
}


// Return the value of the RH1 resistor (in ohms) corresponding to a particular upper bandwidth value (in Hz).
static inline double rH1_from_upper_bandwidth(double upper_bandwidth)
{
	const double log10f = log10(upper_bandwidth);

    return 0.9730 * pow(10.0, (8.0968 - 1.1892 * log10f + 0.04767 * log10f * log10f));
}


// Return the value of the RH2 resistor (in ohms) corresponding to a particular upper bandwidth value (in Hz).
static inline double rH2_from_upper_bandwidth(double upper_bandwidth)
{
    const double log10f = log10(upper_bandwidth);

    return 1.0191 * pow(10.0, (8.1009 - 1.0821 * log10f + 0.03383 * log10f * log10f));
}


// Return the value of the RL resistor (in ohms) corresponding to a particular lower bandwidth value (in Hz).
static inline double rL_from_lower_bandwidth(double lower_bandwidth)
{
    const double log10f = log10(lower_bandwidth);

    if (lower_bandwidth < 4.0) {
        return 1.0061 * pow(10.0, (4.9391 - 1.2088 * log10f + 0.5698 * log10f * log10f +
            0.1442 * log10f * log10f * log10f));
    } else {
        return 1.0061 * pow(10.0, (4.7351 - 0.5916 * log10f + 0.08482 * log10f * log10f));
    }
}


// Return the amplifier upper bandwidth (in Hz) corresponding to a particular value of the resistor RH1 (in Ohms).
static inline double upper_bandwidth_from_rH1(double rH1)
{
    const double a = 0.04767, b = -1.1892, c = 8.0968 - log10(rH1 / 0.9370);
    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Return the amplifier upper bandwidth (in Hz) corresponding to a particular value of the resistor RH2 (in Ohms).
static inline double upper_bandwidth_from_rH2(double rH2)
{
    const double a = 0.03383, b = -1.0821, c = 8.1009 - log10(rH2 / 1.0191);
    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Return the amplifier lower bandwidth (in Hz) corresponding to a particular value of the resistor RL (in Ohms).
static inline double lower_bandwidth_from_rL(double rL)
{
    double a, b, c;

    // Quadratic fit below is invalid for values of RL less than 5.1 kOhm.
    const double valid_rL = (rL < 5100.0) ? 5100.0 : rL;

    if (valid_rL < 30000.0) {
        a = 0.08482;
        b = -0.5916;
        c = 4.7351 - log10(valid_rL / 1.0061);
    } else {
        a = 0.3303;
        b = -1.2100;
        c = 4.9873 - log10(valid_rL / 1.0061);
    }

    return pow(10.0, ((-b - sqrt(b * b - 4 * a * c)) / (2 * a)));
}


// Set the on-chip RH1 and RH2 DAC values appropriately to set a particular amplifier
// upper bandwidth (in Hz).  Return an estimate of the actual upper bandwidth achieved.
static double set_upper_bandwidth(RHSConfigParameters* const p, double upper_bandwidth)
{
	const double RH1Base = 2200.0;
	const double RH1Dac1Unit = 600.0;
	const double RH1Dac2Unit = 29400.0;
	const int RH1Dac1Steps = 63;
	const int RH1Dac2Steps = 31;

	const double RH2Base = 8700.0;
	const double RH2Dac1Unit = 763.0;
	const double RH2Dac2Unit = 38400.0;
	const int RH2Dac1Steps = 63;
	const int RH2Dac2Steps = 31;

	// No upper bandwidths higher than 30 kHz.
	if (upper_bandwidth > 30000.0) {
		upper_bandwidth = 30000.0;
	}

	const double rH1_target = rH1_from_upper_bandwidth(upper_bandwidth);

	p->rH1_sel1 = 0;
	p->rH1_sel2 = 0;
	double rH1_actual = RH1Base;

	for (int i = 0; i < RH1Dac2Steps; ++i) {
		if (rH1_actual < rH1_target - (RH1Dac2Unit - RH1Dac1Unit / 2)) {
			rH1_actual += RH1Dac2Unit;
			++p->rH1_sel2;
		}
	}

	for (int i = 0; i < RH1Dac1Steps; ++i) {
		if (rH1_actual < rH1_target - (RH1Dac1Unit / 2)) {
			rH1_actual += RH1Dac1Unit;
			++p->rH1_sel1;
		}
	}

	const double rH2_target = rH2_from_upper_bandwidth(upper_bandwidth);

	p->rH2_sel1 = 0;
	p->rH2_sel2 = 0;
	double rH2_actual = RH2Base;

	for (int i = 0; i < RH2Dac2Steps; ++i) {
		if (rH2_actual < rH2_target - (RH2Dac2Unit - RH2Dac1Unit / 2)) {
			rH2_actual += RH2Dac2Unit;
			++p->rH2_sel2;
		}
	}

	for (int i = 0; i < RH2Dac1Steps; ++i) {
		if (rH2_actual < rH2_target - (RH2Dac1Unit / 2)) {
			rH2_actual += RH2Dac1Unit;
			++p->rH2_sel1;
		}
	}

	const double actual_upper_bandwidth1 = upper_bandwidth_from_rH1(rH1_actual);
	const double actual_upper_bandwidth2 = upper_bandwidth_from_rH2(rH2_actual);

	// Upper bandwidth estimates calculated from actual RH1 value and actual RH2 value
	// should be very close; we will take their geometric means to get a single number.
	return sqrt(actual_upper_bandwidth1 * actual_upper_bandwidth2);
}


// Set the on-chip RL DAC values appropriately to set a particular amplifier lower bandwidth (in Hz).
// Return an estimate of the actual lower bandwidth achieved.
static double set_lower_bandwidth(RHSConfigParameters* const p, double lower_bandwidth, bool select)
{
    const double RLBase = 3500.0;
    const double RLDac1Unit = 175.0;
    const double RLDac2Unit = 12700.0;
    const double RLDac3Unit = 3000000.0;
    const int RLDac1Steps = 127;
    const int RLDac2Steps = 63;

    // No lower bandwidths higher than 1.5 kHz.
    if (lower_bandwidth > 1500.0) {
    	lower_bandwidth = 1500.0;
    }

    const double rL_target = rL_from_lower_bandwidth(lower_bandwidth);

    int rL_sel1 = 0;
    int rL_sel2 = 0;
    int rL_sel3 = 0;
    double rL_actual = RLBase;

    if (lower_bandwidth < 0.15) {
    	rL_actual += RLDac3Unit;
    	++rL_sel3;
    }

    for (int i = 0; i < RLDac2Steps; ++i) {
    	if (rL_actual < rL_target - (RLDac2Unit - RLDac1Unit / 2)) {
    		rL_actual += RLDac2Unit;
    		++rL_sel2;
    	}
    }

    for (int i = 0; i < RLDac1Steps; ++i) {
    	if (rL_actual < rL_target - (RLDac1Unit / 2)) {
    		rL_actual += RLDac1Unit;
    		++rL_sel1;
    	}
    }

    const double actual_lower_bandwidth = lower_bandwidth_from_rL(rL_actual);

    if (select) {
    	p->rL_A_sel1 = rL_sel1;
    	p->rL_A_sel2 = rL_sel2;
    	p->rL_A_sel3 = rL_sel3;
    } else {
    	p->rL_B_sel1 = rL_sel1;
    	p->rL_B_sel2 = rL_sel2;
    	p->rL_B_sel3 = rL_sel3;
    }

    return actual_lower_bandwidth;
}


// Determine suitable ADC buffer bias and mux bias values based on provided sample rate.
static inline void set_biases_based_on_sample_rate(uint8_t* const adc_buffer_bias, uint8_t* const mux_bias, double sample_rate)
{
	const double adc_sampling_rate = (CONVERT_COMMANDS_PER_SEQUENCE + AUX_COMMANDS_PER_SEQUENCE) * sample_rate;

	if (adc_sampling_rate <= 120000.0) {
		*adc_buffer_bias = 32;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 140000.0) {
		*adc_buffer_bias = 16;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 175000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 40;
	} else if (adc_sampling_rate <= 220000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 32;
	} else if (adc_sampling_rate <= 280000.0) {
		*adc_buffer_bias = 8;
		*mux_bias = 26;
	} else if (adc_sampling_rate <= 350000.0) {
		*adc_buffer_bias = 4;
		*mux_bias = 18;
	} else if (adc_sampling_rate <= 440000.0) {
		*adc_buffer_bias = 3;
		*mux_bias = 16;
	} else if (adc_sampling_rate <= 525000.0) {
		*adc_buffer_bias = 3;
		*mux_bias = 7;
	} else {
		*adc_buffer_bias = 2;
		*mux_bias = 4;
	}
}


// Select the series capacitor used to convert the voltage waveform generated by the on-chip DAC into an AC
// current waveform that stimulates a selected electrode for impedance testing (ZcheckCs100fF, ZcheckCs1pF, or Zcheck10pF).
void set_zcheck_scale(RHSConfigParameters* const p, ZcheckCs scale)
{
	switch (scale) {
	case ZcheckCs100fF:
		p->zcheck_scale = 0x00;		// Cs = 0.1 pF
		break;
	case ZcheckCs1pF:
		p->zcheck_scale = 0x01;		// Cs = 1.0 pF
		break;
	case ZcheckCs10pF:
		p->zcheck_scale = 0x03;		// Cs = 10.0 pF
		break;
	}
}


// Select the amplifier channel for impedance testing.
int set_zcheck_channel(RHSConfigParameters* const p, int channel)
{
	if (channel_out_of_range(channel)) {
		return -1;
	}
	p->zcheck_select = channel;
	return p->zcheck_select;
}


// Set the stimulation current DAC step size.
void set_stim_step_size(RHSConfigParameters* const p, StimStepSize step)
{
	switch (step) {
	case StimStepSizeMin:
		p->stim_step_sel1 = 127;
		p->stim_step_sel2 = 63;
		p->stim_step_sel3 = 3;
		p->stimP_bias = 6;
		p->stimN_bias = 6;
		break;
    case StimStepSize10nA:
    	p->stim_step_sel1 = 64;
        p->stim_step_sel2 = 19;
        p->stim_step_sel3 = 3;
        p->stimP_bias = 6;
        p->stimN_bias = 6;
        break;
    case StimStepSize20nA:
    	p->stim_step_sel1 = 40;
    	p->stim_step_sel2 = 40;
    	p->stim_step_sel3 = 1;
    	p->stimP_bias = 7;
    	p->stimN_bias = 7;
        break;
    case StimStepSize50nA:
    	p->stim_step_sel1 = 64;
    	p->stim_step_sel2 = 40;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 7;
    	p->stimN_bias = 7;
        break;
    case StimStepSize100nA:
    	p->stim_step_sel1 = 30;
    	p->stim_step_sel2 = 20;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 7;
    	p->stimN_bias = 7;
        break;
    case StimStepSize200nA:
    	p->stim_step_sel1 = 25;
    	p->stim_step_sel2 = 10;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 8;
    	p->stimN_bias = 8;
        break;
    case StimStepSize500nA:
    	p->stim_step_sel1 = 101;
    	p->stim_step_sel2 = 3;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 9;
    	p->stimN_bias = 9;
        break;
    case StimStepSize1uA:
    	p->stim_step_sel1 = 98;
    	p->stim_step_sel2 = 1;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 10;
    	p->stimN_bias = 10;
        break;
    case StimStepSize2uA:
    	p->stim_step_sel1 = 94;
    	p->stim_step_sel2 = 0;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 11;
    	p->stimN_bias = 11;
        break;
    case StimStepSize5uA:
    	p->stim_step_sel1 = 38;
    	p->stim_step_sel2 = 0;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 14;
    	p->stimN_bias = 14;
        break;
    case StimStepSize10uA:
    	p->stim_step_sel1 = 15;
    	p->stim_step_sel2 = 0;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 15;
    	p->stimN_bias = 15;
        break;
    case StimStepSizeMax:
    default:
    	p->stim_step_sel1 = 0;
    	p->stim_step_sel2 = 0;
    	p->stim_step_sel3 = 0;
    	p->stimP_bias = 15;
    	p->stimN_bias = 15;
        break;
	}
}


// Set positive stimulation magnitude (0 to 255, in DAC steps) and trim (-128 to +127) for a channel (0-15).
int set_pos_stim_magnitude(RHSConfigParameters* const p, int channel, int magnitude, int trim)
{
	if (channel_out_of_range(channel) || (magnitude < 0) || (magnitude > 255) || (trim < -128) || (trim > 127)) {
		return -1;
	}
	p->pos_current_mag[channel] = magnitude;
	p->pos_current_trim[channel] = trim + 128;
	return 0;
}


// Set negative stimulation magnitude (0 to 255, in DAC steps) and trim (-128 to +127) for a channel (0-15).
int set_neg_stim_magnitude(RHSConfigParameters* const p, int channel, int magnitude, int trim)
{
	if (channel_out_of_range(channel) || (magnitude < 0) || (magnitude > 255) || (trim < -128) || (trim > 127)) {
		return -1;
	}
	p->neg_current_mag[channel] = magnitude;
	p->neg_current_trim[channel] = trim + 128;
	return 0;
}


void set_stim_on(RHSConfigParameters* const p, int channel, bool on)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->stim_on[channel] = on;
}


void set_stim_polarity(RHSConfigParameters* const p, int channel, bool positive)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->stim_pol[channel] = positive;
}


void set_charge_recovery_switch(RHSConfigParameters* const p, int channel, bool connect_to_stim_gnd)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->charge_recovery_switch[channel] = connect_to_stim_gnd;
}


void set_c_l_charge_recovery_enable(RHSConfigParameters* const p, int channel, bool enable)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->c_l_charge_recovery_en[channel] = enable;
}


void set_amp_fast_settle(RHSConfigParameters* const p, int channel, bool settle)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->amp_fast_settle[channel] = settle;
}


void set_amp_f_l_select(RHSConfigParameters* const p, int channel, uint8_t lower_freq_A)
{
	if (channel_out_of_range(channel)) {
		return;
	}
	p->amp_f_l_select[channel] = (lower_freq_A ? 1 : 0);
}


// Set charge recovery current limit
void set_charge_recovery_current_limit(RHSConfigParameters* const p, ChargeRecoveryCurrentLimit limit)
{
    switch (limit) {
    case CurrentLimitMin:
    	p->charge_recovery_current_limit_sel1 = 127;
    	p->charge_recovery_current_limit_sel2 = 63;
    	p->charge_recovery_current_limit_sel3 = 3;
        break;
    case CurrentLimit1nA:
    	p->charge_recovery_current_limit_sel1 = 0;
    	p->charge_recovery_current_limit_sel2 = 30;
    	p->charge_recovery_current_limit_sel3 = 2;
        break;
    case CurrentLimit2nA:
    	p->charge_recovery_current_limit_sel1 = 0;
    	p->charge_recovery_current_limit_sel2 = 15;
    	p->charge_recovery_current_limit_sel3 = 1;
        break;
    case CurrentLimit5nA:
    	p->charge_recovery_current_limit_sel1 = 0;
    	p->charge_recovery_current_limit_sel2 = 31;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit10nA:
    	p->charge_recovery_current_limit_sel1 = 50;
    	p->charge_recovery_current_limit_sel2 = 15;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit20nA:
    	p->charge_recovery_current_limit_sel1 = 78;
    	p->charge_recovery_current_limit_sel2 = 7;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit50nA:
    	p->charge_recovery_current_limit_sel1 = 22;
    	p->charge_recovery_current_limit_sel2 = 3;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit100nA:
    	p->charge_recovery_current_limit_sel1 = 56;
    	p->charge_recovery_current_limit_sel2 = 1;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit200nA:
    	p->charge_recovery_current_limit_sel1 = 71;
    	p->charge_recovery_current_limit_sel2 = 0;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit500nA:
    	p->charge_recovery_current_limit_sel1 = 26;
    	p->charge_recovery_current_limit_sel2 = 0;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimit1uA:
    	p->charge_recovery_current_limit_sel1 = 9;
    	p->charge_recovery_current_limit_sel2 = 0;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    case CurrentLimitMax:
    	p->charge_recovery_current_limit_sel1 = 0;
    	p->charge_recovery_current_limit_sel2 = 0;
    	p->charge_recovery_current_limit_sel3 = 0;
        break;
    }
}


// Set the target voltage for current-limited charge recovery.  The parameter
// v_target should specify a voltage in the range of -1.225 to +1.215 (units = volts).
// Return actual value of target voltage.
double set_charge_recovery_target_voltage(RHSConfigParameters* const p, double v_target)
{
	const double dac_step = 1.225 / 128.0;
	int value = ((int) floor(v_target / dac_step + 0.5)) + 128;
	if (value < 0) value = 0;
	if (value > 255) value = 255;

	p->charge_recovery_DAC = value;
	return dac_step * (value - 128);
}


// Set default values for parameters used to configure RAM registers on RHS2116 chip.
void set_default_rhs_settings(RHSConfigParameters* const p)
{
	// Register 0: Supply Sensor and ADC Buffer Bias Current
	// D[15:12]: X (No Function)
	// D[11:6]: ADC buffer bias
	// D[5:0]: MUX bias
	set_biases_based_on_sample_rate(&p->adc_buffer_bias, &p->mux_bias, p->sample_rate);

	// Register 1: ADC Output Format, DSP Offset Removal, and Auxiliary Digital Outputs
	// D[15:13]: X (No Function)
	// D[12]: digoutOD
	// D[11]: digout2
	// D[10]: digout2 HiZ
	// D[9]: digout1
	// D[8]: digout1 HiZ
	// D[7]: weak MISO
	// D[6]: twoscomp
	// D[5]: absmode
	// D[4]: DSPen
	// D[3:0]: DSP cutoff freq
	set_DigOut_hiZ(p, DigOut1);
	set_DigOut_hiZ(p, DigOut2);
	set_DigOut_hiZ(p, DigOutOD);
	p->weak_miso = true;
	p->twos_comp = false;
	p->abs_mode  = false;
	p->DSP_en    = true;
	set_DSP_cutoff_freq(p, 1.0);

	// Register 2: Impedance Check Control
	// D[15:14]: X (No Function)
	// D[13:8]: Zcheck select
	// D[7]: X (No Function)
	// D[6]: Zcheck DAC power
	// D[5]: Zcheck load
	// D[4:3]: Zcheck scale
	// D[2:1]: X (No Function)
	// D[0]: Zcheck en
	p->zcheck_DAC_power = true;
	p->zcheck_load      = false;
	set_zcheck_scale(p, ZcheckCs100fF);
	p->zcheck_en        = false;
	set_zcheck_channel(p, 0);

	// Register 3:Impedance Check DAC
	// D[15:8]: X (No Function)
	// D[7:0]: Zcheck DAC

	// Register 4-7: On-Chip Amplifier Bandwidth Select
	// R[4]D[15:11]: X (No Function)
	// R[4]D[10:6]: RH1 sel2
	// R[4]D[5:0]: RH1 sel1

	// R[5]D[15:11]: X (No Function)
	// R[5]D[10:6]: RH2 sel2
	// R[5]D[5:0]: RH2 sel1

	// R[6]D[15:14]: X (No Function)
	// R[6]D[13]: RL_A sel3
	// R[6]D[12:7]: RL_A sel2
	// R[6]D[6:0] RL_A sel1

	// R[7]D[15:14]: X (No Function)
	// R[7]D[13]: RL_B sel3
	// R[7]D[12:7]: RL_B sel2
	// R[7]D[6:0]: RL_B sel1

	set_upper_bandwidth(p, 7500.0);
	set_lower_bandwidth(p, 1.0, false);
	set_lower_bandwidth(p, 1000.0, true);

	// Register 8: Individual AC Amplifier Power
	// D[15:0]: AC amp power
	power_up_all_amps(p);

	// Register 9: reserved for future expansion

	// Register 10: Amplifier Fast Settle
	// D[15:0]: amp fast settle
	for (int channel = 0; channel < 16; channel++) {
		set_amp_fast_settle(p, channel, false);
	}

	// Register 11: reserved for future expansion

	// Register 12: Amplifier Lower Cutoff Frequency Select
	// D[15:0]: amp fL select
	for (int channel = 0; channel < 16; channel++) {
		set_amp_f_l_select(p, channel, false);
	}

	// Registers 13-31: reserved for future expansion

	// Register 32: Stimulation Enable A
	// D[15:0]: stim enable A

	// Register 33: Stimulation Enable B
	// D[15:0]: stim enable B
	set_stim_enable(p, true);

	// Register 34: Stimulation Current Step Size
	// D[15]: X (No Function)
	// D[14:13]: step sel3
	// D[12:7]: step sel2
	// D[6:0]: step sel1

	// Register 35: Stimulation Bias Voltages
	// D[15:8]: X (No Function)
	// D[7:4]: stim Pbias
	// D[3:0]: stim Nbias

	set_stim_step_size(p, StimStepSize1uA);

	// Register 36: Current-Limited Charge Recovery Target Voltage
	// D[15:8]: X (No Function)
	// D[7:0]: charge recovery DAC
	set_charge_recovery_target_voltage(p, 0);

	// Register 37: Charge Recovery Current Limit
	// D[15]: X (No Function)
	// D[14:13]: Imax sel3
	// D[12:7]: Imax sel2
	// D[6:0]: Imax sel1
	set_charge_recovery_current_limit(p, CurrentLimit10nA);

	// Register 38: Individual DC Amplifier Power
	// D[15:0]: DC amp power
	power_up_all_DC_amps(p);

	// Register 39: reserved for future expansion

	// Register 40: Compliance Monitor (READ ONLY REGISTER WITH CLEAR)

	// Register 41: reserved for future expansion

	// Register 42: Stimulator On
	// D[15:0]: stim on
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_stim_on(p, channel, false);
	}

	// Register 43: reserved for future expansion

	// Register 44: Stimulator Polarity
	// D[15:0]: stim pol
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_stim_polarity(p, channel, false);
	}

	// Register 45: reserved for future expansion

	// Register 46: Charge Recovery Switch
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_charge_recovery_switch(p, channel, false);
	}

	// Register 47: reserved for future expansion

	// Register 48: Current-Limited Charge Recovery Enable
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_c_l_charge_recovery_enable(p, channel, false);
	}

	// Register 49: reserved for future expansion

	// Register 50: Fault Current Detector (READ ONLY REGISTER)

	// Registers 51-63: reserved for future expansion

	// Registers 64-79: Negative Stimulation Current Magnitude
	// D[15:8]: negative current trim
	// D[7:0]: negative current magnitude
	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_neg_stim_magnitude(p, channel, 0, 0);
	}

	// Registers 80-95: reserved for future expansion

	// Registers 96-111: Positive Stimulation Current magnitude
	// D[15:8]: positive current trim
	// D[7:0]: positive current magnitude

	for (int channel = 0; channel < MAX_NUM_CHANNELS_PER_CHIP; channel++) {
		set_pos_stim_magnitude(p, channel, 0, 0);
	}
}


// Return the value of a selected RAM register (0-111) on the RHS2116 chip,
// based on the current register variables in RHSConfigParameters.
uint16_t get_register_value(const RHSConfigParameters* const p, int reg)
{
	int regout;
	const int ZcheckDac = 128;  // midrange

	switch (reg) {
	case 0:
		regout = (p->adc_buffer_bias << 6) + p->mux_bias;
		break;

	case 1:
        regout = (p->digOutOD << 12) + (p->digOut2 << 11) + (p->digOut2HiZ << 10) + (p->digOut1 << 9) +
            (p->digOut1HiZ << 8) + (p->weak_miso << 7) + (p->twos_comp << 6) + (p->abs_mode << 5) +
            (p->DSP_en << 4) + p->DSP_cutoff_freq;
        break;

	case 2:
        regout = (p->zcheck_select << 8) + (p->zcheck_DAC_power << 6) + (p->zcheck_load << 5) +
            (p->zcheck_scale << 3) + p->zcheck_en;
        break;

	case 3:
        regout = ZcheckDac;
        break;

	case 4:
        regout = (p->rH1_sel2 << 6) + p->rH1_sel1;
        break;

	case 5:
        regout = (p->rH2_sel2 << 6) + p->rH2_sel1;
        break;

	case 6:
		regout = (p->rL_A_sel3 << 13) + (p->rL_A_sel2 << 7) + p->rL_A_sel1;
		break;

	case 7:
		regout = (p->rL_B_sel3 << 13) + (p->rL_B_sel2 << 7) + p->rL_B_sel1;
		break;

	case 8:
        regout = array_to_word(p->amp_pwr);
        break;

	case 10:
		regout = array_to_word(p->amp_fast_settle);
		break;

	case 12:
		regout = array_to_word(p->amp_f_l_select);
		break;

	case 32:
		regout = p->stim_enableA;
		break;

	case 33:
		regout = p->stim_enableB;
		break;

	case 34:
		regout = (p->stim_step_sel3 << 13) + (p->stim_step_sel2 << 7) + p->stim_step_sel1;
		break;

	case 35:
		regout = (p->stimP_bias << 4) + p->stimN_bias;
		break;

	case 36:
		regout = p->charge_recovery_DAC;
		break;

	case 37:
		regout = (p->charge_recovery_current_limit_sel3 << 13) + (p->charge_recovery_current_limit_sel2 << 7) +
		p->charge_recovery_current_limit_sel1;
		break;

	case 38:
		regout = array_to_word(p->dc_amp_pwr);
		break;

	// Register 40 is read only.
	case 42:
		regout = array_to_word(p->stim_on);
		break;

	case 44:
		regout = array_to_word(p->stim_pol);
		break;

	case 46:
		regout = array_to_word(p->charge_recovery_switch);
		break;

	case 48:
		regout = array_to_word(p->c_l_charge_recovery_en);
		break;

	// Register 50 is read only.
	case 64:
		regout = (p->neg_current_trim[0] << 8) + p->neg_current_mag[0];
		break;

	case 65:
		regout = (p->neg_current_trim[1] << 8) + p->neg_current_mag[1];
		break;

	case 66:
		regout = (p->neg_current_trim[2] << 8) + p->neg_current_mag[2];
		break;

	case 67:
		regout = (p->neg_current_trim[3] << 8) + p->neg_current_mag[3];
		break;

	case 68:
		regout = (p->neg_current_trim[4] << 8) + p->neg_current_mag[4];
		break;

	case 69:
		regout = (p->neg_current_trim[5] << 8) + p->neg_current_mag[5];
		break;

	case 70:
		regout = (p->neg_current_trim[6] << 8) + p->neg_current_mag[6];
		break;

	case 71:
		regout = (p->neg_current_trim[7] << 8) + p->neg_current_mag[7];
		break;

	case 72:
		regout = (p->neg_current_trim[8] << 8) + p->neg_current_mag[8];
		break;

	case 73:
		regout = (p->neg_current_trim[9] << 8) + p->neg_current_mag[9];
		break;

	case 74:
		regout = (p->neg_current_trim[10] << 8) + p->neg_current_mag[10];
		break;

	case 75:
		regout = (p->neg_current_trim[11] << 8) + p->neg_current_mag[11];
		break;

	case 76:
		regout = (p->neg_current_trim[12] << 8) + p->neg_current_mag[12];
		break;

	case 77:
		regout = (p->neg_current_trim[13] << 8) + p->neg_current_mag[13];
		break;

	case 78:
		regout = (p->neg_current_trim[14] << 8) + p->neg_current_mag[14];
		break;

	case 79:
		regout = (p->neg_current_trim[15] << 8) + p->neg_current_mag[15];
		break;

	case 96:
		regout = (p->pos_current_trim[0] << 8) + p->pos_current_mag[0];
		break;

	case 97:
		regout = (p->pos_current_trim[1] << 8) + p->pos_current_mag[1];
		break;

	case 98:
		regout = (p->pos_current_trim[2] << 8) + p->pos_current_mag[2];
		break;

	case 99:
		regout = (p->pos_current_trim[3] << 8) + p->pos_current_mag[3];
		break;

	case 100:
		regout = (p->pos_current_trim[4] << 8) + p->pos_current_mag[4];
		break;

	case 101:
		regout = (p->pos_current_trim[5] << 8) + p->pos_current_mag[5];
		break;

	case 102:
		regout = (p->pos_current_trim[6] << 8) + p->pos_current_mag[6];
		break;

	case 103:
		regout = (p->pos_current_trim[7] << 8) + p->pos_current_mag[7];
		break;

	case 104:
		regout = (p->pos_current_trim[8] << 8) + p->pos_current_mag[8];
		break;

	case 105:
		regout = (p->pos_current_trim[9] << 8) + p->pos_current_mag[9];
		break;

	case 106:
		regout = (p->pos_current_trim[10] << 8) + p->pos_current_mag[10];
		break;

	case 107:
		regout = (p->pos_current_trim[11] << 8) + p->pos_current_mag[11];
		break;

	case 108:
		regout = (p->pos_current_trim[12] << 8) + p->pos_current_mag[12];
		break;

	case 109:
		regout = (p->pos_current_trim[13] << 8) + p->pos_current_mag[13];
		break;

	case 110:
		regout = (p->pos_current_trim[14] << 8) + p->pos_current_mag[14];
		break;

	case 111:
		regout = (p->pos_current_trim[15] << 8) + p->pos_current_mag[15];
		break;

	default:
		regout = 0;
	}
	return regout;
}
